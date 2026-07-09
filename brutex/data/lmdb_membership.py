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

Operability (R2-SL-F4-F5): the memory-map ceiling is an operator knob —
explicit ``map_size_mb`` arg > the :data:`SWEEP_CACHE_MAP_MB_ENV` environment
variable > :data:`DEFAULT_MAP_SIZE_MB` — and hitting the ceiling mid-write
surfaces as the actionable domain error :class:`LmdbMapFullError` (naming the
current size + the remedy) instead of a bare ``lmdb.MapFullError`` traceback.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import lmdb

from brutex.models import BruteXError

#: Default memory-map ceiling (grown on demand by LMDB up to this cap).
DEFAULT_MAP_SIZE_MB: Final[int] = 10 * 1024

#: Environment variable overriding the LMDB memory-map ceiling (MiB) for the
#: sweep dedup stores. Resolution order (per store open): explicit
#: ``map_size_mb`` argument > this env var > the opener's own module default
#: (``None``-when-unset resolution, so an unset knob NEVER shrinks this
#: module's 10 GiB default down to the SweepCache tuple-store 2 GiB default,
#: or vice versa). The paired CLI flag is ``--sweep-cache-map-mb``.
SWEEP_CACHE_MAP_MB_ENV: Final[str] = "BRUTEX_SWEEP_CACHE_MAP_MB"

#: Empty membership value — the key's presence IS the membership bit.
_PRESENT: Final[bytes] = b""


class LmdbMembershipError(BruteXError):
    """An LMDB membership-set operation failed."""


class LmdbMapFullError(LmdbMembershipError):
    """The LMDB memory-map ceiling was exhausted mid-write (map full).

    Raised by :meth:`LmdbMembership.add` / :meth:`LmdbMembership.add_many`
    when the underlying ``lmdb.MapFullError`` fires — the store's configured
    ``map_size_mb`` ceiling is too small for the run. The message names the
    CURRENT map size and the remedy (raise ``--sweep-cache-map-mb`` or
    ``BRUTEX_SWEEP_CACHE_MAP_MB``). Stored keys are intact (the failing write
    transaction aborts atomically) and a re-run after growing the map resumes
    idempotently (re-``add`` is a no-op overwrite, CLAUDE.md §2 Rule 7).

    A fatal error by design: it propagates like any infra fault so the sweep
    supervisor's abort path handles it — never swallowed, never retried in a
    loop against a full map.
    """


class LmdbCorruptEnvError(LmdbMembershipError):
    """The LMDB environment could not be opened — corrupt / invalid ``data.mdb``.

    Raised by :meth:`LmdbMembership.open` when the underlying ``lmdb.open``
    fires an ``lmdb.Error`` (e.g. ``lmdb.InvalidError`` / ``MDB_INVALID`` on a
    truncated or byte-XOR'd ``data.mdb``). Typed at this generic leaf so the ONE
    caller that KNOWS the store is a losslessly-rebuildable accelerator — the
    sweep dedup cache (:meth:`brutex.engine.sweep_cache.SweepCache.open`) —
    quarantines the corrupt directory aside and rebuilds a fresh empty store
    (DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001, MIGRATION.md #1640). The other leaf
    consumer (the option-dedup EXACT tier) is NOT a rebuildable accelerator, so
    auto-quarantine is deliberately NOT baked in here — this leaf only names the
    fault; the consumer decides the recovery policy.
    """


def resolve_positive_int_env(
    env_var: str,
    explicit: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Resolve an operator integer knob: explicit arg > *env_var* > ``None``.

    The COMMON env-int resolver for the sweep dedup knobs
    (:data:`SWEEP_CACHE_MAP_MB_ENV` here;
    ``BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY`` in
    :mod:`brutex.engine.sweep_cache`). Returning ``None`` — instead of a
    baked-in default — lets each caller keep its OWN module default, so one
    shared env var can override several stores without an unset knob silently
    collapsing their distinct defaults.

    Parameters
    ----------
    env_var
        Environment-variable name to read when *explicit* is ``None``.
    explicit
        The explicit operator value (e.g. a CLI flag). Wins over the
        environment when not ``None``.
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.

    Returns
    -------
    int | None
        The resolved positive integer, or ``None`` when neither an explicit
        value nor a (non-blank) *env_var* value is present — meaning "keep the
        caller's default".

    Raises
    ------
    ValueError
        If *explicit* is non-positive, or *env_var* holds a non-blank value
        that is not a positive integer. LOUD by contract — a mistyped operator
        knob must never silently fall back (Golden Rule #10). A blank /
        whitespace-only env value is treated as unset.
    """
    if explicit is not None:
        if explicit <= 0:
            raise ValueError(
                f"{env_var}: explicit override must be a positive integer, got {explicit!r}"
            )
        return explicit
    source = os.environ if env is None else env
    raw = source.get(env_var)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{env_var} must be a positive integer, got {raw!r}")
    return value


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
    def open(cls, path: Path, *, map_size_mb: int | None = None) -> Self:
        """Open (creating if absent) the LMDB membership set at *path*.

        Parameters
        ----------
        path
            Directory for the LMDB environment (created with parents).
        map_size_mb
            Memory-map ceiling in MiB; LMDB grows lazily up to this. Operator
            knob resolution (R2-SL-F4-F5): an explicit value wins, else the
            :data:`SWEEP_CACHE_MAP_MB_ENV` environment variable, else
            :data:`DEFAULT_MAP_SIZE_MB` — so a bare ``open`` (the CLI
            combo-dedup stores) is env-tunable without a call-site change,
            and an unset knob keeps this module's own 10 GiB default.

        Raises
        ------
        ValueError
            If the explicit *map_size_mb* or the env-var value is not a
            positive integer (see :func:`resolve_positive_int_env` — loud,
            never a silent fallback).
        LmdbCorruptEnvError
            If ``lmdb.open`` raises an ``lmdb.Error`` — a corrupt / invalid
            ``data.mdb`` (e.g. a truncated or byte-XOR'd environment). The
            fault is typed here, NEVER a raw ``lmdb.Error``, so the SweepCache
            consumer can quarantine + rebuild the accelerator
            (DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001).
        """
        resolved = resolve_positive_int_env(SWEEP_CACHE_MAP_MB_ENV, map_size_mb)
        size_mb = DEFAULT_MAP_SIZE_MB if resolved is None else resolved
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            env = lmdb.open(
                str(path),
                map_size=size_mb * 1024 * 1024,
                max_dbs=1,
                subdir=True,
                create=True,
                lock=True,
            )
        except lmdb.Error as exc:
            raise LmdbCorruptEnvError(
                f"cannot open LMDB membership environment at {path}: {exc} — the "
                f"membership set is a rebuildable dedup accelerator; quarantine the "
                f"directory and reopen to rebuild"
            ) from exc
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
        A map-full write fault is surfaced as the actionable domain error
        :class:`LmdbMapFullError` (see :meth:`add` / :meth:`add_many`), and an
        OPEN-time corruption fault as :class:`LmdbCorruptEnvError` (see
        :meth:`open`). Every OTHER genuine post-open LMDB I/O fault (a bad fd, a
        mid-cursor read fault) still propagates natively as ``lmdb.Error`` for
        the caller's infra-fault handling.
        """
        if self._closed:
            raise LmdbMembershipError("operation on a closed LmdbMembership set")

    def _map_full_message(self) -> str:
        """Build the actionable map-full message naming the CURRENT map size.

        Reads the live ceiling from the environment's own metadata
        (``env.info()['map_size']`` — O(1)), so the reported size is the TRUE
        effective map even if LMDB rounded the requested value.
        """
        map_mb = int(self._env.info()["map_size"]) // (1024 * 1024)
        return (
            f"LMDB membership map full: the {map_mb} MiB memory-map ceiling at "
            f"{self._path} is exhausted — increase --sweep-cache-map-mb (or "
            f"{SWEEP_CACHE_MAP_MB_ENV}); stored keys are intact and a re-run "
            f"after growing the map resumes idempotently."
        )

    def __contains__(self, key: bytes, /) -> bool:
        """True iff *key* is present — O(1) point get (read txn)."""
        self._require_open()
        with self._env.begin(write=False) as txn:
            return txn.get(key) is not None

    def add(self, key: bytes, /) -> None:
        """Insert *key* — O(1) point put (write txn). Re-adding is a no-op.

        Raises
        ------
        LmdbMapFullError
            If the memory-map ceiling is exhausted (``lmdb.MapFullError``) —
            an actionable domain error naming the current map size and the
            ``--sweep-cache-map-mb`` / :data:`SWEEP_CACHE_MAP_MB_ENV` remedy.
            Fatal by design: it propagates to the supervisor abort path.
        """
        self._require_open()
        try:
            with self._env.begin(write=True) as txn:
                txn.put(key, _PRESENT)
        except lmdb.MapFullError as exc:
            raise LmdbMapFullError(self._map_full_message()) from exc

    def add_many(self, keys: Iterable[bytes]) -> int:
        """Insert all *keys* in one write transaction; return the count seen.

        Amortised O(1) per key (single txn commit), the batch path the sink
        uses to persist a whole window's worth of dedup keys at once. On
        success the returned count is unchanged by the map-full guard.

        Raises
        ------
        LmdbMapFullError
            If the memory-map ceiling is exhausted mid-batch — the whole
            write transaction aborts atomically (no partial batch persists),
            with the same actionable message as :meth:`add`.
        """
        self._require_open()
        count = 0
        try:
            with self._env.begin(write=True) as txn:
                for key in keys:
                    txn.put(key, _PRESENT)
                    count += 1
        except lmdb.MapFullError as exc:
            raise LmdbMapFullError(self._map_full_message()) from exc
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
    "SWEEP_CACHE_MAP_MB_ENV",
    "LmdbCorruptEnvError",
    "LmdbMapFullError",
    "LmdbMembership",
    "LmdbMembershipError",
    "resolve_positive_int_env",
]
