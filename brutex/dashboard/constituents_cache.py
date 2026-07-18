"""Disk-cache layer for NSE index constituent lookups.

Makes the best-effort NSE fetch (``brutex.dashboard.nse_constituents``) a
**guarantee after first success**: once a live NSE fetch succeeds the symbols
are written to ``<cache_root>/index_constituents/<INDEX_UPPER_SAFE>.json``,
and every subsequent call — even if the live fetch is blocked, timed out, or
the index is temporarily unavailable — can serve the cached list.

## Honesty (CLAUDE.md §1 / §10)

* The cache itself does **NOT** verify freshness beyond reporting ``as_of``.
  Index membership rebalances semi-annually (CLAUDE.md §7); the operator
  should force-refresh (``force_refresh=True``) after a rebalance or use the
  ``/api/v1/universe/index-constituents?refresh=true`` endpoint.
* The ONLY live-network surface is the injected ``opener`` inside
  :mod:`brutex.dashboard.nse_constituents` (already ``# pragma: no cover``).
  **This module has no ``# pragma: no cover``** and must reach 100%
  line+branch in CI via injected fakes.

## Decision record

DEC-CONSTITUENTS-CACHE-001 (MIGRATION.md #457, 2026-06-04):
powers ``GET /api/v1/universe/index-constituents?symbol=…`` cache-backed
action and the broad-market basket picker (NIFTY 100/200/500) that do not
correspond to any single index row in the instrument store.  See also:
CLAUDE.md §1 (no hallucination), §10 (honest limits), §11 (COMMON · DYNAMIC ·
SCALABLE · AUTOMATED).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from brutex.dashboard.nse_constituents import (
    ConstituentRow,
    constituent_url,
    convention_constituent_url,
    fetch_rows_at_url,
)
from brutex.data.guarded_read import read_regular_text_deadline

__all__ = [
    "ConstituentResult",
    "cache_path",
    "read_cache",
    "resolve_constituents",
    "write_cache",
]

#: Required top-level keys that must be present in a valid cache file.
_REQUIRED_KEYS: frozenset[str] = frozenset({"index", "symbols", "fetched_at", "source_url"})

#: Any character that is not an ASCII letter or digit is replaced by ``_``
#: when building the cache filename — so the key is filesystem-safe.
_UNSAFE_RE: re.Pattern[str] = re.compile(r"[^A-Z0-9]")


@dataclass(frozen=True, slots=True)
class ConstituentResult:
    """Result of a constituent resolution with provenance metadata.

    Attributes
    ----------
    symbols
        The index member symbols (raw, upper-cased).  Empty tuple when the
        index is unknown or the data is unavailable.
    rows
        The index members as ``(symbol, isin)`` pairs — the ISIN-carrying form
        used for rename-proof matching (DEC-INDEX-CONSTITUENTS-NSE-001). Parallel
        to :attr:`symbols`; ``isin`` is ``None`` for a member with no ISIN in the
        source CSV (or when read from a legacy symbols-only cache file). Empty
        tuple when the index is unknown / unavailable.
    source
        Provenance tag — one of:

        ``"cache"``
            Served from the on-disk JSON cache (a previous successful fetch).
        ``"nse"``
            Freshly fetched from NSE and written to the cache.
        ``"unknown-index"``
            The index has no known NSE CSV mapping; nothing was fetched or
            cached.
        ``"unavailable"``
            The live fetch failed (network / block / timeout) and no prior
            cache exists.
    as_of
        ISO-8601 date-string (the ``fetched_at`` date part) indicating
        when the cached data was originally fetched.  ``None`` for
        ``"unknown-index"`` and ``"unavailable"``.
    """

    symbols: tuple[str, ...]
    rows: tuple[ConstituentRow, ...]
    source: str
    as_of: str | None


def cache_path(cache_root: Path, index: str) -> Path:
    """Return the cache file path for *index*.

    Parameters
    ----------
    cache_root
        Root directory for all constituent caches
        (typically ``<lake_root>/index_constituents``).
    index
        Engine index symbol (e.g. ``"NIFTY 100"``).

    Returns
    -------
    Path
        ``<cache_root>/index_constituents/<INDEX_UPPER_SAFE>.json`` where
        ``INDEX_UPPER_SAFE`` is the upper-cased index symbol with every
        non-alphanumeric character replaced by ``_``.

    Examples
    --------
    >>> from pathlib import Path
    >>> cache_path(Path("/tmp/lake"), "NIFTY 100")
    PosixPath('/tmp/lake/index_constituents/NIFTY_100.json')
    """
    safe_name = _UNSAFE_RE.sub("_", index.upper())
    return cache_root / "index_constituents" / f"{safe_name}.json"


def read_cache(cache_root: Path, index: str) -> dict[str, object] | None:
    """Read and validate a constituent cache file.

    Parameters
    ----------
    cache_root
        Root directory for all constituent caches.
    index
        Engine index symbol.

    Returns
    -------
    dict | None
        The parsed cache dict if the file exists, is valid JSON, and has
        all required keys with ``symbols`` a list of strings.  Otherwise
        ``None`` — missing, corrupt, or wrong-shape files all return
        ``None`` (treat as "no cache").

    Notes
    -----
    Only ``(OSError, ValueError)`` are caught — ``json.JSONDecodeError``
    is a ``ValueError`` subclass so it is covered by the same guard.
    """
    path = cache_path(cache_root, index)
    try:
        text = read_regular_text_deadline(path, what="constituents cache read")
        data: object = json.loads(text)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    doc: dict[str, object] = data
    if not _REQUIRED_KEYS.issubset(doc.keys()):
        return None
    if not isinstance(doc["symbols"], list):
        return None
    if not all(isinstance(s, str) for s in doc["symbols"]):
        return None
    return doc


def write_cache(
    cache_root: Path,
    index: str,
    rows: Iterable[ConstituentRow],
    *,
    fetched_at: str,
    source_url: str,
) -> None:
    """Write a constituent list (symbol + ISIN) to the on-disk cache.

    Parameters
    ----------
    cache_root
        Root directory for all constituent caches.
    index
        Engine index symbol.
    rows
        The constituent ``(symbol, isin)`` rows to persist. Persisted as a
        ``symbols`` list (backward-compatible with the legacy symbols-only reader)
        PLUS a parallel ``isins`` list (``null`` for a member with no ISIN) so the
        ISIN-primary match (DEC-INDEX-CONSTITUENTS-NSE-001) survives a cache hit.
    fetched_at
        ISO-8601 timestamp string representing when the data was fetched.
    source_url
        The NSE CSV URL the rows were fetched from (for audit / freshness
        traceability).

    Notes
    -----
    Parent directories are created with ``parents=True, exist_ok=True`` so
    a fresh ``cache_root`` works without a prior ``mkdir``.  The JSON is
    written with ``sort_keys=True, indent=2`` for human-readability and
    stable diffs. The ``isins`` key is additive — a pre-existing reader that
    only knows ``symbols`` still works.
    """
    path = cache_path(cache_root, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    payload = {
        "index": index,
        "symbols": [row.symbol for row in rows],
        "isins": [row.isin for row in rows],
        "fetched_at": fetched_at,
        "source_url": source_url,
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def resolve_constituents(
    index: str,
    *,
    cache_root: Path,
    opener: Callable[[str], str] | None = None,
    now: datetime | None = None,
    force_refresh: bool = False,
) -> ConstituentResult:
    """Resolve an index's constituent symbols, with disk-cache backing.

    This is the main orchestrator — it implements the
    *guarantee-after-first-success* contract:

    1. If the index has no known NSE CSV mapping (``constituent_url``
       returns ``None``) → ``ConstituentResult((), "unknown-index", None)``.
       No fetch, no cache write.
    2. If NOT ``force_refresh`` and a valid cache file exists → serve it
       immediately as ``source="cache"``.  The opener is **never called**
       on a cache hit.
    3. Otherwise attempt a live fetch via
       :func:`brutex.dashboard.nse_constituents.fetch_constituent_symbols`:

       a. **Success (non-empty list):** write the cache, return
          ``source="nse"``.
       b. **``OSError`` or empty list:** if a (possibly stale) cache exists
          serve it as ``source="cache"``; otherwise return
          ``ConstituentResult((), "unavailable", None)``.

    Parameters
    ----------
    index
        Engine index symbol (e.g. ``"NIFTY 100"``).
    cache_root
        Root directory for the on-disk constituent cache.
    opener
        Injected HTTP fetcher ``(url) -> csv_text`` passed straight through
        to :func:`~brutex.dashboard.nse_constituents.fetch_constituent_symbols`.
        Defaults to the real urllib opener (``# pragma: no cover`` live surface
        inside that module).  Tests inject a fake to avoid any network access.
    now
        Injected current timestamp (a timezone-aware ``datetime``).  When
        ``None`` (production default) ``datetime.now(tz=UTC)`` is used.
        Injecting a fixed value makes tests fully deterministic.
    force_refresh
        When ``True``, skip the cache check and always attempt a live fetch.
        A stale cache is still used as the fallback if the live fetch fails.

    Returns
    -------
    ConstituentResult
        Immutable result with ``symbols``, ``source``, and ``as_of``.
    """
    # Resolve the CSV URL: the verified/override mapping first, then NSE's public
    # filename convention as a self-validating fallback (DEC-INDEX-CONSTITUENTS-
    # COVERAGE-001) so EVERY index — not just the ~10 explicitly mapped ones —
    # gets a fetch attempt. A wrong convention guess 404s → honest "unavailable",
    # never WRONG data (the symbols always come from NSE's response).
    url = constituent_url(index) or convention_constituent_url(index)

    cached = read_cache(cache_root, index)

    if not force_refresh and cached is not None:
        return _result_from_cache(cached)

    if url is None:
        # No verified mapping AND no convention candidate (a symbolless index, e.g.
        # all-punctuation) → genuinely unknown. A stale cache was already served above.
        if cached is not None:
            return _result_from_cache(cached)
        return ConstituentResult(symbols=(), rows=(), source="unknown-index", as_of=None)

    # Attempt live fetch of the resolved URL (verified mapping OR convention
    # candidate). ``fetch_rows_at_url`` fetches the EXACT url — not re-derived
    # via ``constituent_url`` (which would drop the convention candidate) — and
    # carries each member's ISIN for rename-proof matching.
    try:
        rows = fetch_rows_at_url(url, opener=opener)
    except OSError:
        # Network / block / timeout: serve stale cache if present, else unavailable.
        if cached is not None:
            return _result_from_cache(cached)
        return ConstituentResult(symbols=(), rows=(), source="unavailable", as_of=None)

    if not rows:
        # Empty live response: fall back to stale cache if present.
        if cached is not None:
            return _result_from_cache(cached)
        return ConstituentResult(symbols=(), rows=(), source="unavailable", as_of=None)

    # Fresh success: write cache (symbols + isins) and return.
    ts = (now if now is not None else datetime.now(tz=UTC)).isoformat()
    write_cache(cache_root, index, rows, fetched_at=ts, source_url=url)
    return ConstituentResult(
        symbols=tuple(row.symbol for row in rows), rows=tuple(rows), source="nse", as_of=ts
    )


def _result_from_cache(cached: dict[str, object]) -> ConstituentResult:
    """Build a ``source="cache"`` result from a validated cache document.

    Reconstructs the ``(symbol, isin)`` rows from the parallel ``symbols`` +
    ``isins`` arrays. A legacy cache file (written before
    DEC-INDEX-CONSTITUENTS-NSE-001) has no ``isins`` key, or one whose length
    does not line up with ``symbols`` — in either case the ISINs degrade to
    ``None`` (the match then falls back to ticker-only for that file until the
    next refresh re-writes it with ISINs).
    """
    as_of = str(cached["fetched_at"]).split("T")[0]
    raw_symbols = cached["symbols"]
    syms = tuple(str(s) for s in raw_symbols) if isinstance(raw_symbols, list) else ()
    raw_isins = cached.get("isins")
    if isinstance(raw_isins, list) and len(raw_isins) == len(syms):
        isins: tuple[str | None, ...] = tuple(
            str(value) if value is not None else None for value in raw_isins
        )
    else:
        isins = tuple(None for _ in syms)
    rows = tuple(
        ConstituentRow(symbol=sym, isin=isin) for sym, isin in zip(syms, isins, strict=True)
    )
    return ConstituentResult(symbols=syms, rows=rows, source="cache", as_of=as_of)
