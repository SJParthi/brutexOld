"""LMDB-backed exact-membership set — the off-heap, O(1)-space dedup tier.

The persistent exact-confirm tier for
:class:`~brutex.data.option_dedup.OptionDedupIndex` (and any byte-keyed dedup):
a content key is a fixed 32-byte digest, stored as an LMDB key with an empty
value (a pure set). LMDB is memory-mapped, so membership state lives **off the
Python heap** — per-operation RAM is O(1) regardless of how many keys are
stored (the operator O(1)-space requirement; MIGRATION.md #342). LMDB's B+tree
makes ``__contains__`` / ``add`` O(log n) in theory but O(1) in practice for a
dense working set (the same envelope as :mod:`brutex.orchestrator.store`,
proven by ``tests/bench/test_lmdb_membership_o1.py``).

It satisfies the :class:`~brutex.data.option_dedup.ExactMembership` protocol
(``__contains__`` + ``add``, positional-only), so it drops straight into
``OptionDedupIndex(exact=...)`` with no rewrite — survivor of the in-memory
``set`` for runs that must persist / exceed RAM.

Idempotent: re-``add`` of a present key is a no-op overwrite (same empty
value), so a duplicate re-pull never corrupts the set (CLAUDE.md §2 Rule 7).
The environment is a context manager; ``add_many`` batches a whole window in
one write transaction (amortised O(1) per key).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import lmdb

from brutex.models import BruteXError

#: Default memory-map ceiling (grown on demand by LMDB up to this cap).
DEFAULT_MAP_SIZE_MB: Final[int] = 10 * 1024

#: Empty membership value — the key's presence IS the membership bit.
_PRESENT: Final[bytes] = b""


class LmdbMembershipError(BruteXError):
    """An LMDB membership-set operation failed."""


class LmdbMembership:
    """An LMDB-backed ``set[bytes]`` (membership only) — the persistent dedup tier.

    Open via :meth:`open` (a context manager). Keys are raw content-hash
    bytes (e.g. the 32-byte blake3 digest from
    :func:`~brutex.data.option_dedup.option_bar_key`).
    """

    def __init__(self, *, path: Path, env: lmdb.Environment) -> None:
        self._path = path
        self._env = env
        self._closed = False

    @classmethod
    def open(cls, path: Path, *, map_size_mb: int = DEFAULT_MAP_SIZE_MB) -> Self:
        """Open (creating if absent) the LMDB membership set at *path*.

        Parameters
        ----------
        path
            Directory for the LMDB environment (created with parents).
        map_size_mb
            Memory-map ceiling in MiB; LMDB grows lazily up to this.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        env = lmdb.open(
            str(path),
            map_size=map_size_mb * 1024 * 1024,
            max_dbs=1,
            subdir=True,
            create=True,
            lock=True,
        )
        return cls(path=path, env=env)

    def __enter__(self) -> Self:
        """Enter the context — return ``self``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the environment on context exit."""
        del exc_type, exc_val, exc_tb
        self.close()

    def close(self) -> None:
        """Release the LMDB environment (idempotent)."""
        if not self._closed:
            self._env.close()
            self._closed = True

    def _require_open(self) -> None:
        """Raise :class:`LmdbMembershipError` if the environment is closed.

        The deterministic worst case — using the set after :meth:`close`.
        Genuine LMDB I/O faults (map full, corruption) propagate natively as
        ``lmdb.Error`` for the orchestrator's infra-fault handling.
        """
        if self._closed:
            raise LmdbMembershipError("operation on a closed LmdbMembership set")

    def __contains__(self, key: bytes, /) -> bool:
        """True iff *key* is present — O(1) point get (read txn)."""
        self._require_open()
        with self._env.begin(write=False) as txn:
            return txn.get(key) is not None

    def add(self, key: bytes, /) -> None:
        """Insert *key* — O(1) point put (write txn). Re-adding is a no-op."""
        self._require_open()
        with self._env.begin(write=True) as txn:
            txn.put(key, _PRESENT)

    def add_many(self, keys: Iterable[bytes]) -> int:
        """Insert all *keys* in one write transaction; return the count seen.

        Amortised O(1) per key (single txn commit), the batch path the sink
        uses to persist a whole window's worth of dedup keys at once.
        """
        self._require_open()
        count = 0
        with self._env.begin(write=True) as txn:
            for key in keys:
                txn.put(key, _PRESENT)
                count += 1
        return count

    def __len__(self) -> int:
        """Return the number of stored keys — O(1) via the LMDB stat entry count.

        LMDB tracks ``entries`` in the main DB's B+tree metadata, so this is a
        constant-time read (no scan). Used by
        :meth:`brutex.engine.sweep_cache.SweepCache.open` to size / capacity-check
        the L1 Bloom before repopulating it from this ground-truth store.
        """
        self._require_open()
        with self._env.begin(write=False) as txn:
            return int(txn.stat()["entries"])

    def iter_keys(self) -> Iterator[bytes]:
        """Yield every stored key in LMDB key order — O(1) per key.

        A read cursor walk over the whole set. Used on a resume to rebuild the
        in-RAM L1 Bloom from this durable ground truth (the persisted digests
        the previous run recorded), so a Bloom-negative can never mask a
        genuinely-recorded combo (CLAUDE.md §2 Rules 4 & 7, idempotence). The
        whole read transaction is held open for the duration of the walk, so the
        caller MUST fully consume (or close) the iterator promptly.
        """
        self._require_open()
        with self._env.begin(write=False) as txn:
            cursor = txn.cursor()
            for key in cursor.iternext(keys=True, values=False):
                yield bytes(key)


__all__ = [
    "DEFAULT_MAP_SIZE_MB",
    "LmdbMembership",
    "LmdbMembershipError",
]
