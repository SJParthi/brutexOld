"""Last-good disk cache for the NIFTY TOTAL MARKET constituent CSV.

DEC-NTM-CONSTITUENT-CACHE-001. The ``/universe`` page's full ~750-stock universe
is NOT in the vendor instrument master — it is the **NIFTY TOTAL MARKET broad-market
constituents**, fetched best-effort from ``niftyindices.com`` at every keyless
instrument load (``instrument_loader._ntm_admission``). NSE bot-blocks + frequently
times out that host, and on a miss the universe SILENTLY cratered from ~750 stocks
to ~211 (the base F&O-eligible set, without the reference constituents) — the
operator saw "211 STOCKS" with ``constituent resolution failed: The read operation
timed out``.

This module persists the **last-good constituent CSV** to disk so a transient
fetch failure (timeout / bot-block / empty) falls back to it instead of dropping
the constituents — the universe stays at ~750. The cache is refreshed on every
successful live fetch, so it only ever serves a recent-but-stale list when the
live source is unreachable.

Design (CLAUDE.md §11 COMMON + Golden Rule #10):

* **Pure + fully testable** — every function takes an explicit ``cache_dir`` (no
  hidden ``$HOME`` lookup inside the I/O), so tests drive it against a tmp dir at
  100% line+branch. The default production dir is :func:`default_cache_dir`
  (``~/.brutex/cache/``), resolved by the caller.
* **Best-effort, never raises** — caching is a hygiene layer, NOT a correctness
  dependency: a write/read failure (permission, disk full, corrupt file) returns
  cleanly so it can NEVER break the keyless instrument load it serves.
* **Honest envelope** — the cache only mitigates a TRANSIENT live failure; it
  cannot invent constituents that were never fetched once. A first-ever boot with
  no cache AND a dead NSE still degrades to the base set (with an honest note).
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import read_regular_text_deadline

#: The cache filename under the cache dir. One file, the raw last-good CSV text.
_CACHE_FILENAME = "ntm_constituents.csv"

#: Sidecar lock file for the CROSS-PROCESS revalidation single-flight
#: (DEC-NTM-REVALIDATE-CROSSPROC-001). Dot-prefixed so no cache reader ever
#: mistakes it for content; it is only ever opened + ``flock``-ed, never replaced,
#: so the inode is stable (the DEC-LAKE-PARTITION-FLOCK-001 sidecar precedent).
REVALIDATE_LOCK_FILENAME: Final[str] = ".ntm_revalidate.lock"

#: The tri-state outcome of :func:`revalidate_lock` — ``"acquired"`` (this process
#: holds the exclusive cross-process lock; run the refresh), ``"held_elsewhere"``
#: (a PEER process is refreshing right now; skip — its atomic save lands for
#: everyone), or ``"unavailable"`` (the lock file could not even be created —
#: read-only/odd cache dir; the caller fails OPEN to the pre-fix unguarded refresh,
#: which is idempotent-harmless, so the guard can never make things worse).
RevalidateLockVerdict = Literal["acquired", "held_elsewhere", "unavailable"]


@dataclass(frozen=True, slots=True)
class CachedConstituents:
    """A loaded last-good constituent CSV plus its age on disk.

    ``age_seconds`` is ``now - mtime`` of the cache file, **clamped at 0.0** so a
    future mtime (clock skew / a copied file) never yields a negative age. The
    cache-first instrument load (``instrument_loader._resolve_constituent_csv``)
    compares it against the TTL to decide the instant-serve hot path vs a refresh.
    """

    csv_text: str
    age_seconds: float


def default_cache_dir() -> Path:
    """Return the per-user cache dir (``~/.brutex/cache/``) — the production default.

    Mirrors the other per-user state locations (``~/.brutex/quota/``,
    ``~/.brutex/``). The caller resolves this and passes it explicitly so the
    I/O functions stay pure + tmp-dir-testable.
    """
    return Path.home() / ".brutex" / "cache"


def _cache_file(cache_dir: Path) -> Path:
    """The CSV cache file path under *cache_dir*."""
    return cache_dir / _CACHE_FILENAME


def save_constituent_csv(csv_text: str, *, cache_dir: Path) -> bool:
    """Persist a freshly-fetched constituent CSV as the new last-good cache.

    Best-effort + atomic: writes to a ``.tmp`` sibling then ``os.replace``-renames
    it over the cache file, so a concurrent reader never sees a half-written file.
    An EMPTY *csv_text* is NOT cached (it would overwrite a good list with nothing).
    NEVER raises — a write failure (permission / disk full) returns ``False`` so a
    caching hiccup can never break the instrument load.

    Parameters
    ----------
    csv_text
        The freshly-fetched constituent CSV text. Empty ⇒ no-op (returns ``False``).
    cache_dir
        The directory to write the cache into (created if absent).

    Returns
    -------
    bool
        ``True`` iff the cache was written; ``False`` on an empty input or any I/O
        failure (both non-fatal).
    """
    if not csv_text:
        return False
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = _cache_file(cache_dir)
        # COMMON guard-free atomic write (tmp→fsync→os.replace→cleanup, CLAUDE.md §11,
        # DEC-ATOMIC-WRITE-CONSOLIDATE-001) — best-effort: a raw OSError is caught below
        # and returns False (a cache hiccup never breaks the instrument load), NOT the
        # lake's LakeWriteHalt.
        atomic_write_simple(target, lambda tmp: tmp.write_text(csv_text, encoding="utf-8"))
    except OSError:
        return False
    return True


def load_cached_constituent_csv(*, cache_dir: Path) -> str | None:
    """Return the last-good cached constituent CSV, or ``None`` when unavailable.

    The age-agnostic public read API: callers that just need the cached text (and not
    its freshness) use this; the cache-first instrument load uses the age-aware sibling
    :func:`load_cached_constituent_with_age` instead (DEC-NTM-CACHE-FIRST-001). Retained
    as the simple convenience read (it does not ``stat`` for an mtime).

    Best-effort: a missing file, an unreadable file (permission), or an empty file
    all return ``None`` so the caller cleanly degrades to the base universe. NEVER
    raises.

    Parameters
    ----------
    cache_dir
        The directory the cache was written into.

    Returns
    -------
    str | None
        The cached CSV text, or ``None`` when there is no usable cache.
    """
    try:
        text = read_regular_text_deadline(_cache_file(cache_dir), what="NTM constituent cache read")
    except OSError:
        return None
    return text or None


def load_cached_constituent_with_age(
    *, cache_dir: Path, now: float | None = None
) -> CachedConstituents | None:
    """Return the cached CSV **and its age in seconds**, or ``None`` when unavailable.

    The age-aware sibling of :func:`load_cached_constituent_csv`, powering the
    cache-first instrument load (DEC-NTM-CACHE-FIRST-001): the loader serves a cache
    YOUNGER than the TTL instantly without any network call (the ultra-fast O(1) hot
    path), and only refreshes an older/absent one.

    Best-effort, NEVER raises (same contract as :func:`load_cached_constituent_csv`):
    a missing file, an unreadable file (permission), or an empty file all return
    ``None`` so the caller degrades cleanly. A single ``read_text``+``stat`` reads the
    body and its mtime; ``now`` is injectable for deterministic tests (defaults to
    :func:`time.time`). The age is clamped at ``0.0`` (a future mtime never goes
    negative).

    Parameters
    ----------
    cache_dir
        The directory the cache was written into.
    now
        Override the "current time" (epoch seconds) for tests. ``None`` ⇒
        :func:`time.time`.

    Returns
    -------
    CachedConstituents | None
        The cached CSV text + its age in seconds, or ``None`` when there is no usable
        cache.
    """
    file = _cache_file(cache_dir)
    try:
        text = read_regular_text_deadline(file, what="NTM constituent cache read")
        mtime = file.stat().st_mtime
    except OSError:
        return None
    if not text:
        return None
    current = time.time() if now is None else now
    age = current - mtime
    return CachedConstituents(csv_text=text, age_seconds=age if age > 0.0 else 0.0)


@contextmanager
def revalidate_lock(*, cache_dir: Path) -> Generator[RevalidateLockVerdict, None, None]:
    """Try the NON-BLOCKING cross-process NTM revalidation lock; yield the verdict.

    DEC-NTM-REVALIDATE-CROSSPROC-001 — the cross-process arm of the revalidation
    single-flight (the named follow-up of DEC-NTM-STALE-WHILE-REVALIDATE-001, whose
    in-flight flag is PROCESS-local: multiple uvicorn workers could each spawn one
    refresh against the slow niftyindices host). An advisory exclusive
    :func:`fcntl.flock` is attempted ONCE, non-blocking, on the stable sidecar
    ``<cache_dir>/.ntm_revalidate.lock``:

    * ``"acquired"`` — this process holds the lock for the ``with`` block; run the
      refresh. Released by closing the fd on exit (the kernel also releases it on
      process death — no stale-lock recovery needed, the
      DEC-LAKE-PARTITION-FLOCK-001 precedent).
    * ``"held_elsewhere"`` — a peer process is refreshing RIGHT NOW; the caller
      skips (never queues — the refresh outcome is shared via the peer's atomic
      cache save, so waiting would only double the fetch).
    * ``"unavailable"`` — the lock file could not be created/opened (read-only
      cache dir, exotic FS). The caller fails OPEN to the pre-fix unguarded
      refresh: the guard is an optimisation, and an unguarded double-refresh is
      idempotent-harmless (atomic save) — never worse than before the guard.

    NEVER raises (same best-effort contract as every other function here); O(1) —
    one ``mkdir`` + ``open`` + ``flock`` attempt, no polling, no waiting.
    ``fcntl.flock`` is POSIX (Linux + macOS — the Track-1 hardware envelope);
    Windows is out of scope, exactly like the lake partition lock.

    Parameters
    ----------
    cache_dir
        The NTM cache directory (created if absent); the sidecar lives inside it.

    Yields
    ------
    RevalidateLockVerdict
        The tri-state outcome; the lock (when acquired) is held for the block.
    """
    fd: int | None = None
    verdict: RevalidateLockVerdict = "acquired"
    try:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            # 0o600 — owner-only: a private coordination sidecar, never
            # world-readable (the partition-lock permission precedent).
            fd = os.open(cache_dir / REVALIDATE_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # The peer holds it (EWOULDBLOCK). BlockingIOError is an OSError
            # subclass, so this arm MUST precede the general one below.
            verdict = "held_elsewhere"
        except OSError:
            verdict = "unavailable"
        yield verdict
    finally:
        # Closing the fd releases the advisory lock (when held) and frees the fd in
        # one step, on every (success or error) exit. A ``held_elsewhere`` open fd
        # is closed too — it never carried the lock.
        if fd is not None:
            os.close(fd)


__all__ = [
    "REVALIDATE_LOCK_FILENAME",
    "CachedConstituents",
    "RevalidateLockVerdict",
    "default_cache_dir",
    "load_cached_constituent_csv",
    "load_cached_constituent_with_age",
    "revalidate_lock",
    "save_constituent_csv",
]
