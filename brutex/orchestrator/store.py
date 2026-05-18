"""LMDB-backed WorkItemStore — O(1) persistent dedup cache for the 4-phase pipeline.

Implements:
* ``NFR-PERF-002`` (bar lookup O(1) — generalised here to work-item lookup)
* ``NFR-PERF-003`` (combo dedup O(1))
* ``FR-BF-004`` (dedup cache persistent across process restarts)
* ``NFR-REL-001`` / ``NFR-REL-002`` (idempotent reruns)

## Why LMDB?

- **Memory-mapped** — reads are memcpy from the file-system cache, no
  copies into Python-heap bytes objects for the scan path.
- **B+tree** — O(log n) worst case but effectively O(1) for the working
  set (every in-flight ingest sits comfortably in RAM).
- **Crash-safe** — ACID semantics via LMDB's copy-on-write MVCC. If
  BruteX dies mid-write, no corruption is possible.
- **No daemon** — unlike Redis / DuckDB server / etc., LMDB is embedded.
  Zero ops overhead.
- **Single-writer / multi-reader** — perfect for our architecture where
  one supervisor writes and many workers read the dedup cache.

## Layout

One LMDB **environment** per run (rooted at `data/runs/<run_id>/store.lmdb/`).
Inside the environment, one **sub-database** per :class:`IngestPhase`
so per-phase scans are efficient:

```
data/runs/abc123/store.lmdb/
├── data.mdb          (the actual B+tree data)
├── lock.mdb          (reader-writer lock table)
└── <sub-dbs>         A_INSTRUMENTS / B_EXPIRIES / C_CONTRACTS / D_CANDLES
```

Keys inside each sub-db are the work item's ``content_hash`` encoded
as raw 32 bytes (not hex — saves 50% space and halves B+tree page
fanout). Values are JSON-encoded :class:`WorkItem` payloads.

## API surface

- :meth:`WorkItemStore.open` — create or resume an environment
- :meth:`WorkItemStore.put` — persist a WorkItem (insert or update)
- :meth:`WorkItemStore.get` — read one item by (phase, content_hash)
- :meth:`WorkItemStore.exists_terminal` — fast dedup check
- :meth:`WorkItemStore.iter_phase` — streaming scan of a whole phase
- :meth:`WorkItemStore.count_by_state` — dashboard stats
- :meth:`WorkItemStore.close` — release the environment

All writes happen inside a single LMDB transaction, so they are
atomic and durable. The store is **thread-safe for readers** but
writers must be serialized by the caller (matches the single-
supervisor design).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import lmdb

from brutex.models import BruteXError
from brutex.orchestrator.state import (
    IngestPhase,
    QuarantineReason,
    WorkItem,
    WorkItemState,
    is_terminal,
)

# ────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────

#: Default map size for the LMDB environment, in MiB. Allocated as
#: virtual memory so this is cheap — physical pages are faulted in
#: on demand. Sized to hold ~10M work items × ~1 KB each.
DEFAULT_MAP_SIZE_MB: Final[int] = 10 * 1024

#: Maximum number of named sub-databases inside one environment.
#: Must be at least ``len(IngestPhase)`` (currently 4).
MAX_DBS: Final[int] = 8

#: Expected length (bytes) of a content hash as stored in the B+tree.
#: sha256 → 32 raw bytes.
CONTENT_HASH_BYTES: Final[int] = 32


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class StoreError(BruteXError):
    """Base class for :class:`WorkItemStore` errors."""


class StoreCorruptionError(StoreError):
    """Raised when a stored WorkItem fails to deserialise.

    Indicates either disk corruption or an incompatible schema change
    (a field was added to :class:`WorkItem` since the data was
    written). The fix is usually to migrate the store or delete it
    and re-run the phase from scratch.
    """


class StoreKeyError(StoreError):
    """Raised when a ``content_hash`` is malformed."""


# ────────────────────────────────────────────────────────────────────────
# Serialisation
# ────────────────────────────────────────────────────────────────────────


def _encode_key(content_hash: str) -> bytes:
    """Convert a 64-char hex content_hash into 32 raw bytes for LMDB."""
    if len(content_hash) != CONTENT_HASH_BYTES * 2:
        raise StoreKeyError(
            f"content_hash must be {CONTENT_HASH_BYTES * 2} hex chars (got {len(content_hash)})"
        )
    try:
        return bytes.fromhex(content_hash)
    except ValueError as exc:
        raise StoreKeyError(f"content_hash is not valid hex: {exc}") from exc


def _serialize_item(item: WorkItem) -> bytes:
    """Serialise a :class:`WorkItem` to JSON bytes."""
    data = {
        "item_id": item.item_id,
        "phase": item.phase.value,
        "content_hash": item.content_hash,
        "params": [list(kv) for kv in item.params],
        "state": item.state.value,
        "retry_count": item.retry_count,
        "quarantine_reason": (item.quarantine_reason.value if item.quarantine_reason else None),
        "resolved_by_human": item.resolved_by_human,
        "created_at": item.created_at.isoformat(),
        "last_transition_at": item.last_transition_at.isoformat(),
    }
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _deserialize_item(raw: bytes) -> WorkItem:
    """Reconstruct a :class:`WorkItem` from JSON bytes."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreCorruptionError(f"Store entry is not valid JSON: {exc}") from exc

    required = {
        "item_id",
        "phase",
        "content_hash",
        "params",
        "state",
        "retry_count",
        "quarantine_reason",
        "resolved_by_human",
        "created_at",
        "last_transition_at",
    }
    missing = required - set(data.keys())
    if missing:
        raise StoreCorruptionError(f"Store entry missing required fields: {sorted(missing)}")

    try:
        return WorkItem(
            item_id=str(data["item_id"]),
            phase=IngestPhase(data["phase"]),
            content_hash=str(data["content_hash"]),
            params=tuple(tuple(kv) for kv in data["params"]),
            state=WorkItemState(data["state"]),
            retry_count=int(data["retry_count"]),
            quarantine_reason=(
                QuarantineReason(data["quarantine_reason"]) if data["quarantine_reason"] else None
            ),
            resolved_by_human=bool(data["resolved_by_human"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_transition_at=datetime.fromisoformat(data["last_transition_at"]),
        )
    except (ValueError, TypeError) as exc:
        raise StoreCorruptionError(f"Store entry has malformed field: {exc}") from exc


# ────────────────────────────────────────────────────────────────────────
# WorkItemStore
# ────────────────────────────────────────────────────────────────────────


class WorkItemStore:
    """LMDB-backed persistent store for :class:`WorkItem` objects.

    Thread-safe for concurrent reads; writes must be serialized by
    the caller. In the BruteX architecture, a single supervisor actor
    owns all writes while many worker actors do concurrent dedup
    checks via :meth:`exists_terminal`.

    Use as a context manager::

        with WorkItemStore.open(Path("data/runs/abc/store.lmdb")) as store:
            store.put(item)
            if store.exists_terminal(IngestPhase.A_INSTRUMENTS, some_hash):
                continue  # dedup hit, skip the call
            for w in store.iter_phase(IngestPhase.A_INSTRUMENTS):
                ...
    """

    def __init__(
        self,
        *,
        path: Path,
        env: lmdb.Environment,
        # DEC-P0-017 (PR #39): lmdb-py exports the Database type as `_Database`
        # (no public alias in the binding stubs); intentional use of the
        # private name. Same rationale at the dbs assignment in `open()` below.
        dbs: dict[IngestPhase, lmdb._Database],  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        self._path = path
        self._env = env
        self._dbs = dbs
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        map_size_mb: int = DEFAULT_MAP_SIZE_MB,
        read_only: bool = False,
    ) -> WorkItemStore:
        """Create or resume the store at *path*.

        Parameters
        ----------
        path
            Directory that holds (or will hold) the LMDB environment.
            Parent directories are created as needed.
        map_size_mb
            Max virtual memory reserved for the environment, in MiB.
            Cheap to over-allocate (only faults in physical pages on
            demand). Defaults to :data:`DEFAULT_MAP_SIZE_MB`.
        read_only
            If True, opens in read-only mode — useful for dashboard
            processes that shouldn't mutate state. Writers get
            :exc:`lmdb.Error` if they try.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        env = lmdb.open(
            str(path),
            map_size=map_size_mb * 1024 * 1024,
            max_dbs=MAX_DBS,
            subdir=True,
            readonly=read_only,
            create=not read_only,
            # Always keep the reader lock table live — even read-only
            # processes need it to coordinate with writers.
            lock=True,
        )
        # Open one sub-database per phase. LMDB has a quirk where sub-db
        # handles acquired inside a read-only transaction don't persist
        # across transactions in all versions of the Python binding.
        # For read-only mode we instead use `env.open_db(key)` with NO
        # txn argument — the binding opens an implicit internal txn
        # and returns a handle that's valid for the env's lifetime.
        dbs: dict[IngestPhase, lmdb._Database] = {}  # pyright: ignore[reportPrivateUsage]  # DEC-P0-017: see __init__ rationale above
        if read_only:
            for phase in IngestPhase:
                dbs[phase] = env.open_db(phase.value.encode("utf-8"), create=False)
        else:
            with env.begin(write=True) as txn:
                for phase in IngestPhase:
                    dbs[phase] = env.open_db(phase.value.encode("utf-8"), txn=txn, create=True)
        return cls(path=path, env=env, dbs=dbs)

    # ── Context manager ────────────────────────────────────────────

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying LMDB environment."""
        if not self._closed:
            self._env.close()
            self._closed = True

    # ── Writes ─────────────────────────────────────────────────────

    def put(self, item: WorkItem) -> None:
        """Insert or update *item*.

        Uses the item's ``(phase, content_hash)`` as the compound key.
        A put with an existing key OVERWRITES (expected — state
        transitions update the same row).

        Raises
        ------
        StoreError
            If the store is closed.
        StoreKeyError
            If the item's content_hash is malformed.
        """
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        key = _encode_key(item.content_hash)
        value = _serialize_item(item)
        with self._env.begin(write=True) as txn:
            txn.put(key, value, db=self._dbs[item.phase])

    def put_many(self, items: list[WorkItem]) -> None:
        """Batch insert/update — one transaction for all items.

        Significantly faster than repeated :meth:`put` for bulk
        operations like seeding a phase's initial PENDING items.
        """
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        with self._env.begin(write=True) as txn:
            for item in items:
                key = _encode_key(item.content_hash)
                value = _serialize_item(item)
                txn.put(key, value, db=self._dbs[item.phase])

    # ── Reads ──────────────────────────────────────────────────────

    def get(self, phase: IngestPhase, content_hash: str) -> WorkItem | None:
        """Read one item by hash. Returns None if absent.

        Time complexity: O(log n) on the B+tree; effectively O(1)
        for any reasonable working-set size (the full current ingest
        fits comfortably in a single LMDB page cache).
        """
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        key = _encode_key(content_hash)
        with self._env.begin() as txn:
            raw = txn.get(key, db=self._dbs[phase])
        if raw is None:
            return None
        # lmdb returns memoryview by default; coerce to bytes for the
        # deserialiser's narrower type signature.
        return _deserialize_item(bytes(raw))

    def exists_terminal(self, phase: IngestPhase, content_hash: str) -> bool:
        """Fast dedup check — True iff an item exists AND is terminal.

        Used by every worker before making a Dhan API call. If this
        returns True, the call is skipped entirely (deduplication
        guarantee + O(1) lookup).
        """
        item = self.get(phase, content_hash)
        return item is not None and is_terminal(item.state)

    def iter_phase(self, phase: IngestPhase) -> Iterator[WorkItem]:
        """Stream every :class:`WorkItem` currently stored in *phase*.

        Implementation detail: opens a single LMDB cursor and yields
        deserialised items one at a time. Reads are consistent (MVCC
        snapshot at the moment ``iter_phase`` is called — concurrent
        writes during iteration are NOT visible, which is what we want).
        """
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        with self._env.begin() as txn:
            cursor = txn.cursor(db=self._dbs[phase])
            for _, raw in cursor:
                # Coerce memoryview → bytes for the deserialiser.
                yield _deserialize_item(bytes(raw))

    def count_by_state(self, phase: IngestPhase) -> dict[WorkItemState, int]:
        """Return a histogram of states within *phase*.

        Dashboard uses this for the "123 success / 4 no-data /
        1 quarantined" summary line on each phase card.
        """
        counts: Counter[WorkItemState] = Counter()
        for item in self.iter_phase(phase):
            counts[item.state] += 1
        return dict(counts)

    def count_phase(self, phase: IngestPhase) -> int:
        """Return total item count within *phase* (all states)."""
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        with self._env.begin() as txn:
            stat = txn.stat(db=self._dbs[phase])
        return int(stat["entries"])

    # ── Maintenance ────────────────────────────────────────────────

    def flush(self, force: bool = False) -> None:
        """Flush pending writes to disk.

        LMDB's default sync mode already flushes on transaction
        commit, but this can be called explicitly for belt-and-braces
        durability (e.g., right before a long-running pause).
        """
        if self._closed:
            return
        self._env.sync(force=force)

    def delete_phase(self, phase: IngestPhase) -> int:
        """Clear every item from *phase*. Returns the number of
        items deleted. Used for manual phase re-run from scratch —
        dangerous; the CLI that exposes this requires ``--force``.
        """
        if self._closed:
            raise StoreError(f"Store {self._path} is closed")
        count = 0
        with self._env.begin(write=True) as txn:
            # `drop` clears a sub-database without deleting it
            count = txn.stat(db=self._dbs[phase])["entries"]
            txn.drop(self._dbs[phase], delete=False)
        return int(count)


__all__ = [
    "CONTENT_HASH_BYTES",
    "DEFAULT_MAP_SIZE_MB",
    "MAX_DBS",
    "StoreCorruptionError",
    "StoreError",
    "StoreKeyError",
    "WorkItemStore",
]
