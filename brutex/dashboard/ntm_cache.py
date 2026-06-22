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

import time
from dataclasses import dataclass
from pathlib import Path

from brutex.data.atomic_io import atomic_write_simple

#: The cache filename under the cache dir. One file, the raw last-good CSV text.
_CACHE_FILENAME = "ntm_constituents.csv"


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
        text = _cache_file(cache_dir).read_text(encoding="utf-8")
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
        text = file.read_text(encoding="utf-8")
        mtime = file.stat().st_mtime
    except OSError:
        return None
    if not text:
        return None
    current = time.time() if now is None else now
    age = current - mtime
    return CachedConstituents(csv_text=text, age_seconds=age if age > 0.0 else 0.0)


__all__ = [
    "CachedConstituents",
    "default_cache_dir",
    "load_cached_constituent_csv",
    "load_cached_constituent_with_age",
    "save_constituent_csv",
]
