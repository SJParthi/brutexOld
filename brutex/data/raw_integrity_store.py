"""Durable sidecar store for :class:`~brutex.data.raw_integrity.RawIntegrityRecord`.

DEC-RAW-DATA-INTEGRITY-001 / DEC-PULL-CONTENTION-ANTISTALL-001. The per-window
raw-payload hash (:mod:`brutex.data.raw_integrity`) is only useful as a PROVABLE
audit if it SURVIVES the pull that captured it — so this is its durable home:
**append-only JSON-lines sidecars** under ``<lake_root>/raw_integrity/``, SHARDED
**per ``groww_symbol``** at ``<lake_root>/raw_integrity/<sha256(groww_symbol)>.jsonl``.

Why a SEPARATE sidecar (not the candle lake, not the window ledger):

* **The candle lake is untouched (the hard requirement).** This store lives
  OUTSIDE ``bars/`` / ``options/`` under its own ``raw_integrity/`` directory, so
  no candle partition, no lake schema, and nothing the lake scanner reads is
  changed — the integrity layer is purely ADDITIVE proof alongside the data.
* **Append-only over an in-place rewrite (the lower-risk additive option).** A
  JSON-line APPEND per captured window is O(record) and never reads/rewrites the
  whole file — it can never lose or corrupt prior records on a crash mid-write
  (a half-written trailing line is simply skipped on load). The window ledger
  was considered, but extending its fixed Parquet schema with a
  ``raw_payload_hash`` column would (a) touch a load-bearing, heavily-tested
  lake table and (b) couple the proof to the ledger's load/save/migrate path —
  strictly more risk than an isolated sidecar. The sidecar keys by the SAME
  ``(exchange, segment, groww_symbol, interval, window_start, window_end)``
  coordinates, so a future ledger join is trivial if ever wanted.

Why SHARDED per ``groww_symbol`` (DEC-PULL-CONTENTION-ANTISTALL-001 GAP #14):
the previous single shared ``records.jsonl`` serialised EVERY window completion —
across all ~750 symbols — behind one process-global lock + one no-timeout
cross-process ``flock``, so a parallel fan-out queued every fetch worker through
ONE file. Sharding by ``groww_symbol`` means two DIFFERENT symbols' captures never
contend (distinct files, distinct locks); :func:`load_records` globs ALL ``*.jsonl``
shards (and the legacy ``records.jsonl``, so old sidecars still load). The
cross-process ``flock`` is BOUNDED (``LOCK_EX | LOCK_NB`` retried under a short
deadline) so a wedged peer can never pin a fetch worker — exhaustion is the existing
best-effort SKIP (the hash is additive proof; a dropped record only costs a
re-verify, never data).

Reliability (never crash a pull): :func:`append_record` is best-effort at the
call site (the ingest hook guards it); :func:`load_records` degrades to an empty
result on a missing / unreadable / malformed file (a structured WARNING, never
an exception) — a lost integrity sidecar costs a re-verify, never data.

The record-per-window APPEND is O(1); the one-shot :func:`load_records` is
O(records) (latest-wins per key), paid only by the verify path, never the hot
pull.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path

import structlog

from brutex.data.guarded_read import read_regular_text
from brutex.data.raw_integrity import (
    HASH_ALGORITHM,
    RawIntegrityError,
    RawIntegrityRecord,
    build_record,
)

#: Structured logger — surfaces a corrupt/absent integrity sidecar into ``/logs``.
_LOG = structlog.get_logger("brutex.data.raw_integrity_store")

#: The integrity sidecar directory + legacy single-file name under the lake root
#: (NEVER under ``bars/`` / ``options/`` — it is proof alongside the data, not lake
#: data). The legacy ``records.jsonl`` is still LOADED (older sidecars), but new
#: appends write to a per-``groww_symbol`` shard.
INTEGRITY_DIRNAME: str = "raw_integrity"
INTEGRITY_FILENAME: str = "records.jsonl"

#: Bounded cross-process ``flock`` deadline (seconds) for ONE shard append. Short —
#: a single short JSON-line append holds the lock for microseconds, so a peer holding
#: it longer than this is wedged; exhaustion degrades to a best-effort skip (the hash
#: is additive proof). Sharding makes contention rare; this only bounds the worst case.
SHARD_FLOCK_TIMEOUT_SECONDS: float = 5.0

#: Sleep between non-blocking re-acquire attempts (seconds) for the shard ``flock``.
SHARD_FLOCK_POLL_SECONDS: float = 0.02

#: Serialise concurrent appends from the parallel window workers WITHIN this process.
#: An APPEND of one short JSON line under an exclusive lock is atomic enough for a
#: sidecar (each line is independently parseable; a partial trailing line on a crash
#: is skipped on load). Module-level so every store instance for a given process shares
#: it (the conservative, never-interleave choice). Now that the sidecar is SHARDED per
#: ``groww_symbol``, distinct symbols' shards are distinct FILES — but they still share
#: this one in-process lock; the lock is held only for the microsecond byte-append, so
#: it is not a contention bottleneck (the cross-process ``flock`` per shard is what
#: distinct symbols no longer contend on). CROSS-process safety is the BOUNDED advisory
#: :func:`fcntl.flock` taken on each shard fd inside :func:`append_record`.
_APPEND_LOCK = threading.Lock()


def integrity_dir(lake_root: Path) -> Path:
    """Return the integrity directory for *lake_root* (``<lake_root>/raw_integrity``)."""
    return lake_root / INTEGRITY_DIRNAME


def integrity_path(lake_root: Path) -> Path:
    """Return the LEGACY single-file sidecar path (``<lake_root>/raw_integrity/records.jsonl``).

    Retained for back-compat: pre-sharding sidecars wrote here, and
    :func:`load_records` still reads it. New appends go to a per-``groww_symbol``
    :func:`shard_path` instead.
    """
    return integrity_dir(lake_root) / INTEGRITY_FILENAME


def shard_path(lake_root: Path, groww_symbol: str) -> Path:
    """Return the per-symbol shard path (``<lake_root>/raw_integrity/<sha256(symbol)>.jsonl``).

    Keyed by ``sha256(groww_symbol)`` so each symbol's captures land in their OWN file
    (two different symbols never contend on one lock — DEC-PULL-CONTENTION-ANTISTALL-001
    GAP #14) and the same symbol ALWAYS maps to the same shard (append order preserved
    per symbol, so latest-wins-per-key still holds). O(1).
    """
    digest = hashlib.sha256(groww_symbol.encode("utf-8")).hexdigest()
    return integrity_dir(lake_root) / f"{digest}.jsonl"


def _record_to_json(record: RawIntegrityRecord) -> str:
    """Serialise one record to a single compact JSON line (no embedded newline).

    ``issued_symbol`` (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001) is written ONLY
    when set — a divergent-form fetch (the ``%26`` fallback recovery) — so the
    common plain-form record line stays BYTE-IDENTICAL to the pre-field format
    (back-compat both ways: old readers ignore the extra key, new readers default
    an absent key to ``None``).
    """
    payload = {
        "exchange": record.exchange,
        "segment": record.segment,
        "groww_symbol": record.groww_symbol,
        "interval": record.interval,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "payload_hash": record.payload_hash,
        "row_count": record.row_count,
        "algorithm": record.algorithm,
        "captured_at": (record.captured_at.isoformat() if record.captured_at is not None else None),
    }
    if record.issued_symbol is not None:
        payload["issued_symbol"] = record.issued_symbol
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _try_flock_bounded(fd: int) -> bool:
    """Take an exclusive shard ``flock`` NON-BLOCKING, retried under a short deadline.

    Loops ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` catching ``BlockingIOError`` (a peer
    process holds it) + sleeping :data:`SHARD_FLOCK_POLL_SECONDS` until taken or
    :data:`SHARD_FLOCK_TIMEOUT_SECONDS` elapses. Returns ``True`` on acquire, ``False``
    on deadline exhaustion (the caller then SKIPS the additive record — never blocks the
    fetch worker; DEC-PULL-CONTENTION-ANTISTALL-001 GAP #14). O(1) on the uncontended
    fast path (one non-blocking call).
    """
    deadline = time.monotonic() + SHARD_FLOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(SHARD_FLOCK_POLL_SECONDS)


def append_record(record: RawIntegrityRecord, lake_root: Path) -> None:
    """Append *record* as one JSON line to its per-symbol shard (crash-safe append).

    Creates the ``raw_integrity/`` directory if absent and APPENDS one line to the
    ``record.groww_symbol`` shard (:func:`shard_path`). The write is serialised on BOTH
    levels (so a CLI ``brutex fetch`` and a dashboard pull sharing one
    ``BRUTEX_LAKE_ROOT`` can never interleave bytes):

    * a process-local :data:`_APPEND_LOCK` (``threading.Lock``) serialises the
      parallel window workers WITHIN this process, and
    * a CROSS-process advisory :func:`fcntl.flock` taken on the open shard fd for the
      duration of the write serialises SEPARATE processes — BOUNDED via
      ``LOCK_EX | LOCK_NB`` retried under :data:`SHARD_FLOCK_TIMEOUT_SECONDS` so a
      wedged peer can NEVER pin the fetch worker (DEC-PULL-CONTENTION-ANTISTALL-001).
      The lock is held on the SAME append-mode fd that is written, which is safe here
      because — unlike a Parquet partition — the sidecar is never replaced
      (``os.replace`` would swap the inode); it is only ever appended to, so the fd's
      inode is stable for the lifetime of the lock. On deadline exhaustion the record
      is SKIPPED + logged (best-effort: the hash is additive proof; a dropped record
      only costs a re-verify, never data).

    SHARDING (GAP #14): distinct ``groww_symbol`` shards are distinct files with distinct
    locks, so two symbols' captures never contend — only same-symbol appends queue, and
    only on the microsecond byte-write.

    O(record) — never reads or rewrites prior lines, so a crash mid-append can at
    worst leave a partial trailing line (skipped on load), never corrupt earlier
    records. The candle lake is never touched.

    The CALLER (the ingest capture hook) is responsible for best-effort guarding
    this so a sidecar write failure can never halt a pull — this function does
    the durable write and lets an :class:`OSError` propagate for the guard to
    swallow.
    """
    directory = integrity_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    line = _record_to_json(record)
    path = shard_path(lake_root, record.groww_symbol)
    with _APPEND_LOCK, path.open("a", encoding="utf-8") as handle:
        # Bounded advisory exclusive lock across processes; released automatically when
        # the ``with`` block closes the fd. POSIX (Linux + macOS — the Track-1 envelope);
        # Windows is out of scope, as for the partition lock.
        if not _try_flock_bounded(handle.fileno()):
            _LOG.warning(
                "raw_integrity.append_lock_timeout",
                groww_symbol=record.groww_symbol,
                interval=record.interval,
                window_start=record.window_start,
                window_end=record.window_end,
                timeout_seconds=SHARD_FLOCK_TIMEOUT_SECONDS,
            )
            return  # best-effort skip — the additive hash is not worth blocking a pull
        handle.write(line + "\n")


def _record_from_obj(obj: object) -> RawIntegrityRecord | None:
    """Reconstruct a record from one parsed JSON object, or ``None`` if malformed.

    A line missing a required key, or carrying a wrong-typed value, returns
    ``None`` (skipped on load) rather than raising — one bad line never sinks the
    whole sidecar. ``captured_at`` is parsed back to a tz-aware datetime when
    present + parseable, else left ``None`` (provenance only, never part of the
    hash, so an unparseable stamp is non-fatal).
    """
    if not isinstance(obj, dict):
        return None
    try:
        captured_raw = obj.get("captured_at")
        captured_at: datetime | None = None
        if isinstance(captured_raw, str):
            try:
                captured_at = datetime.fromisoformat(captured_raw)
            except ValueError:
                captured_at = None
        # LEGACY records carry no ``issued_symbol`` key → ``None`` (the issued form
        # IS the key symbol; DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 back-compat) —
        # a non-string value is treated the same (provenance-adjacent, never fatal).
        issued_raw = obj.get("issued_symbol")
        issued_symbol = issued_raw if isinstance(issued_raw, str) else None
        return RawIntegrityRecord(
            exchange=str(obj["exchange"]),
            segment=str(obj["segment"]),
            groww_symbol=str(obj["groww_symbol"]),
            interval=str(obj["interval"]),
            window_start=str(obj["window_start"]),
            window_end=str(obj["window_end"]),
            payload_hash=str(obj["payload_hash"]),
            row_count=int(obj["row_count"]),
            algorithm=str(obj.get("algorithm", HASH_ALGORITHM)),
            captured_at=captured_at,
            issued_symbol=issued_symbol,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_records(lake_root: Path) -> dict[tuple[str, str, str, str, str, str], RawIntegrityRecord]:
    """Load every integrity shard as a ``{key: latest record}`` map (latest-wins).

    Globs ALL ``*.jsonl`` files under ``raw_integrity/`` — the per-``groww_symbol``
    shards AND the legacy single ``records.jsonl`` (so pre-sharding sidecars still load,
    DEC-PULL-CONTENTION-ANTISTALL-001 GAP #14). Shards are read in a DETERMINISTIC name
    order so a key duplicated ACROSS shards resolves stably; WITHIN one shard a later
    line wins (append order). A given window's key only ever lands in ONE shard (its
    symbol's), so cross-shard collisions only arise from a legacy + sharded mix — handled
    by the deterministic order.

    **Never raises** (reliability): a missing directory / no shards is an empty map; an
    unreadable shard logs a WARNING and is skipped (the others still load); a malformed
    individual line is skipped (the append-only format means a crash can only damage the
    trailing line). When the same window appears more than once (a re-pull re-captures
    it), the LAST (most recent) record for that key wins.

    O(records) — paid only by the verify path, never the hot pull.
    """
    directory = integrity_dir(lake_root)
    if not directory.is_dir():
        return {}
    records: dict[tuple[str, str, str, str, str, str], RawIntegrityRecord] = {}
    for path in sorted(directory.glob("*.jsonl")):
        try:
            text = read_regular_text(path)
        except OSError as exc:
            _LOG.warning("raw_integrity.load_failed", path=str(path), error=str(exc))
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # a partial/corrupt trailing line — skip, never crash
            record = _record_from_obj(obj)
            if record is not None:
                records[record.key] = record
    return records


def find_record(
    lake_root: Path,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    window_start: str,
    window_end: str,
) -> RawIntegrityRecord | None:
    """Look up the latest recorded :class:`RawIntegrityRecord` for one window.

    Convenience over :func:`load_records` for the verify path (re-fetch ONE
    window then compare). Returns ``None`` when the window was never captured —
    the honest "no proof on file" state (e.g. a window pulled before this layer
    shipped; the :func:`~brutex.data.raw_integrity.verify_record` envelope).
    """
    return load_records(lake_root).get(
        (exchange, segment, groww_symbol, interval, window_start, window_end)
    )


def append_records(records: Iterable[RawIntegrityRecord], lake_root: Path) -> int:
    """Append every record in *records* — returns the count appended.

    A thin batch convenience over :func:`append_record` (one durable line each),
    used where several windows are captured together; returns how many were
    written so the caller can log the count.
    """
    written = 0
    for record in records:
        append_record(record, lake_root)
        written += 1
    return written


def make_capture_hook(
    lake_root: Path, *, exchange: str, segment: str, groww_symbol: str
) -> Callable[[Mapping[str, object], str, str, str, str], None]:
    """Return a BEST-EFFORT raw-payload capture hook for one instrument's fetch.

    The function the ingest layer injects as
    :data:`~brutex.adapters.groww.candles.RawPayloadHook` (DEC-RAW-DATA-INTEGRITY-001).
    On each fetched window it (1) builds the integrity record
    (:func:`~brutex.data.raw_integrity.build_record` — hash of the RAW payload at
    capture) for the bound ``(exchange, segment, groww_symbol)`` plus the per-call
    ``(interval, window_start, window_end, issued_symbol)`` the hook receives, and
    (2) APPENDS it to the sidecar (:func:`append_record`). *issued_symbol* is the
    symbol form the fetch was PHYSICALLY issued with
    (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001): on a ``%26`` fallback recovery it
    diverges from the bound key symbol, and recording it is what lets the verify
    path re-issue the byte-identical call; ``build_record`` normalises the common
    identical case to ``None`` so plain-form records stay byte-identical.

    **It can NEVER halt or corrupt a pull** (the hard requirement): every error —
    a malformed payload (:class:`~brutex.data.raw_integrity.RawIntegrityError`) or
    a sidecar write failure (:class:`OSError`) — is caught and logged at WARNING,
    then swallowed, so the candle fetch+store proceeds exactly as if the hook were
    absent. The hash is additive PROOF, never a gate.

    Per fetched window the work is O(payload) (one canonicalise+hash) + O(record)
    (one append) — off the rate-limited critical path's correctness, additive only.
    """

    def _capture(
        payload: Mapping[str, object],
        interval: str,
        window_start: str,
        window_end: str,
        issued_symbol: str,
    ) -> None:
        try:
            record = build_record(
                payload,
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                interval=interval,
                window_start=window_start,
                window_end=window_end,
                issued_symbol=issued_symbol,
            )
            append_record(record, lake_root)
        except (RawIntegrityError, OSError) as exc:
            # Best-effort: a bad payload / sidecar write failure must NEVER halt or
            # corrupt the pull (the candle write is the source of truth; the hash is
            # additive proof). Log + swallow so the fetch proceeds unaffected.
            _LOG.warning(
                "raw_integrity.capture_failed",
                groww_symbol=groww_symbol,
                interval=interval,
                window_start=window_start,
                window_end=window_end,
                error=str(exc),
            )

    return _capture


#: A per-symbol capture-hook factory (DEC-RAW-DATA-INTEGRITY-001). Given the
#: RESOLVED ``(exchange, segment, groww_symbol)`` of ONE instrument's fetch, it
#: returns the :data:`~brutex.adapters.groww.candles.RawPayloadHook` to fire per
#: window for THAT instrument. This is the COMMON wiring primitive the live pull
#: needs: a SINGLE adapter / option-ingest run pulls MANY symbols through one
#: object, but :func:`make_capture_hook` binds ONE ``groww_symbol`` — so the
#: live build sites inject this factory (bound to the lake root) and the fetch
#: paths call it with each resolved symbol, building the correctly-attributed
#: per-symbol hook at the point the symbol is known.
CaptureHookFactory = Callable[
    [str, str, str], Callable[[Mapping[str, object], str, str, str, str], None]
]


def make_capture_hook_factory(lake_root: Path) -> CaptureHookFactory:
    """Return a :data:`CaptureHookFactory` bound to *lake_root*.

    The factory takes the RESOLVED ``(exchange, segment, groww_symbol)`` of one
    fetch and returns a best-effort :func:`make_capture_hook` for it — so the
    live spot adapter (:class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter`)
    and the live option ingest (:func:`~brutex.adapters.groww.option_ingest.run_groww_option_ingest`),
    which each pull MANY symbols through ONE object, can build a correctly
    per-symbol-attributed capture hook at the call site where the symbol is
    resolved (the single bound :func:`make_capture_hook` cannot — it fixes one
    ``groww_symbol``). The returned per-symbol hooks are themselves best-effort
    (they never halt a pull), so wiring the factory can never make a pull fail.
    """

    def _factory(
        exchange: str, segment: str, groww_symbol: str
    ) -> Callable[[Mapping[str, object], str, str, str, str], None]:
        return make_capture_hook(
            lake_root, exchange=exchange, segment=segment, groww_symbol=groww_symbol
        )

    return _factory


__all__ = [
    "INTEGRITY_DIRNAME",
    "INTEGRITY_FILENAME",
    "SHARD_FLOCK_POLL_SECONDS",
    "SHARD_FLOCK_TIMEOUT_SECONDS",
    "CaptureHookFactory",
    "append_record",
    "append_records",
    "find_record",
    "integrity_dir",
    "integrity_path",
    "load_records",
    "make_capture_hook",
    "make_capture_hook_factory",
    "shard_path",
]
