"""Best-effort NSE index → constituent-stock resolution (metadata helper).

DEC-UNIVERSE-NSE-CONSTITUENTS-001 (MIGRATION.md #445). Powers the
``/universe`` page "⊕ tick its constituents" action: when an INDEX is
selected, fetch its member stocks and auto-tick the ones that exist in
our F&O-eligible universe, ready for "⤓ Pull N selected".

## Honesty (CLAUDE.md §1 no-hallucination, §10 honest limits)

This is **NOT Dhan data** — the Dhan instrument master does not carry
index membership. NSE publishes per-index constituent CSVs (e.g.
``ind_nifty50list.csv``) under
``https://nsearchives.nseindia.com/content/indices/``. Those CSVs are
the ONLY source here, and the live fetch is **best-effort**:

* this build environment NETWORK-BLOCKS ``nseindia.com`` ("Host not in
  allowlist"), so the live fetch CANNOT be tested/verified in CI;
* NSE actively bot-blocks those endpoints, so even on the operator's
  machine it may fail.

Therefore the live fetch (and ONLY the live fetch) is marked
``# pragma: no cover`` and the route degrades gracefully — on ANY
network/block/timeout it returns an honest "unavailable" note pointing
at the **"Pull ALL F&O stocks"** superset, never a 500.

We deliberately do NOT hardcode any constituent list from memory:
index membership rebalances semi-annually (CLAUDE.md §7 — NIFTY 50
rebalance March/September), so a baked-in list would silently go wrong.
Only the index→filename MAPPING is hardcoded, and it is DOCUMENTED as
"verify against NSE; may change".

## Pure vs. live split (so 100% of the logic is testable)

* :func:`parse_symbols` — PURE CSV parse (no I/O). 100% unit-tested.
* :func:`constituent_url` — PURE mapping (no I/O). 100% unit-tested.
* :func:`fetch_constituent_symbols` / :func:`members_in_universe` —
  take an injected ``opener`` callable so the network can be faked in
  tests; the DEFAULT real urllib opener is the single ``# pragma: no
  cover`` live surface.
"""

from __future__ import annotations

import csv
import io
import os
import urllib.request
from collections.abc import Callable, Iterable
from typing import Final

#: Base URL for NSE per-index constituent CSVs. Overridable via the
#: ``BRUTEX_NSE_CONSTITUENTS_BASE_URL`` env var so an operator behind a
#: mirror / proxy can repoint it without a code change (CLAUDE.md §11
#: DYNAMIC). Best-effort — see module docstring (NSE bot-blocks + this
#: build env network-blocks the host).
NSE_CONSTITUENTS_BASE_URL: Final[str] = os.environ.get(
    "BRUTEX_NSE_CONSTITUENTS_BASE_URL",
    "https://nsearchives.nseindia.com/content/indices",
).rstrip("/")

#: Engine index symbol → NSE constituent-CSV filename.
#:
#: BEST-EFFORT — **verify against NSE; NSE may rename/retire these.** We
#: intentionally cover only the handful we are reasonably confident of;
#: every other index resolves to ``None`` ("unknown-index") and the UI
#: falls back to the "Pull ALL F&O stocks" superset. Keys are upper-cased
#: engine symbols; aliases (e.g. ``NIFTY NEXT 50`` and ``NIFTYNXT50``)
#: both map to the same file so the dashboard's display symbol and the
#: bare engine symbol both resolve.
INDEX_CSV_FILENAMES: Final[dict[str, str]] = {
    "NIFTY": "ind_nifty50list.csv",
    "NIFTY 50": "ind_nifty50list.csv",
    "NIFTY50": "ind_nifty50list.csv",
    "BANKNIFTY": "ind_niftybanklist.csv",
    "NIFTY BANK": "ind_niftybanklist.csv",
    "FINNIFTY": "ind_niftyfinservicelist.csv",
    "NIFTY FIN SERVICE": "ind_niftyfinservicelist.csv",
    "MIDCPNIFTY": "ind_niftymidcapselect.csv",
    "NIFTY MIDCAP SELECT": "ind_niftymidcapselect.csv",
    "NIFTYNXT50": "ind_niftynext50list.csv",
    "NIFTY NEXT 50": "ind_niftynext50list.csv",
}


def parse_symbols(csv_text: str) -> list[str]:
    """Parse the ``Symbol`` column from an NSE constituent CSV.

    PURE — no I/O. The NSE constituent CSV columns are
    ``Company Name, Industry, Symbol, Series, ISIN Code``; this reads the
    ``Symbol`` column, strips whitespace, drops blanks (incl. trailing
    blank lines), and de-duplicates **preserving first-seen order**.
    Symbols carrying an ampersand (e.g. ``M&M``, ``GVT&D``) survive
    intact because the CSV reader handles quoting/commas correctly.

    Parameters
    ----------
    csv_text
        The raw CSV text (a header row plus one row per constituent).

    Returns
    -------
    list[str]
        Constituent symbols, upper-cased, de-duplicated, order-preserved.
        ``[]`` if the text is empty or has no ``Symbol`` column.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []
    # Tolerate header whitespace/case drift (e.g. " Symbol ").
    symbol_key: str | None = None
    for name in reader.fieldnames:
        if name is not None and name.strip().lower() == "symbol":
            symbol_key = name
            break
    if symbol_key is None:
        return []
    seen: set[str] = set()
    symbols: list[str] = []
    for row in reader:
        raw = row.get(symbol_key)
        if raw is None:
            continue
        sym = raw.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    return symbols


def constituent_url(index_symbol: str) -> str | None:
    """Map an engine index symbol to its NSE constituent-CSV URL.

    PURE — no I/O. Case-insensitive lookup against
    :data:`INDEX_CSV_FILENAMES`.

    Returns
    -------
    str | None
        The full CSV URL, or ``None`` for an index we have no mapping
        for ("unknown-index").
    """
    filename = INDEX_CSV_FILENAMES.get(index_symbol.strip().upper())
    if filename is None:
        return None
    return f"{NSE_CONSTITUENTS_BASE_URL}/{filename}"


def _default_opener(url: str) -> str:  # pragma: no cover - live network only
    """Fetch *url* and return its text body (the real urllib opener).

    LIVE ONLY (``# pragma: no cover``): NSE bot-blocks these endpoints
    and this build environment network-blocks ``nseindia.com``, so this
    cannot be exercised in CI. A browser-like ``User-Agent`` + ``Accept``
    + a short timeout give the best-effort fetch its best chance on the
    operator's machine. Callers inject a fake ``opener`` in tests.
    """
    request = urllib.request.Request(  # noqa: S310 - https NSE host, best-effort
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/csv,application/csv,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        body: bytes = response.read()
    return body.decode("utf-8")


def fetch_constituent_symbols(
    index_symbol: str,
    *,
    opener: Callable[[str], str] | None = None,
) -> list[str]:
    """Resolve *index_symbol* → URL → fetched → parsed constituent symbols.

    Parameters
    ----------
    index_symbol
        Engine index symbol (e.g. ``"NIFTY"``, ``"BANKNIFTY"``).
    opener
        Injected fetcher ``(url) -> csv_text``. Defaults to the real
        urllib opener (:func:`_default_opener`, ``# pragma: no cover``).
        Tests pass a fake to exercise the success path offline.

    Returns
    -------
    list[str]
        Parsed constituent symbols, or ``[]`` for an unknown index.

    Notes
    -----
    This does NOT swallow fetch errors — a network/block/timeout raised
    by *opener* propagates so the caller (the route) can map it to the
    honest "unavailable" note. Only the unknown-index case returns ``[]``
    here.
    """
    url = constituent_url(index_symbol)
    if url is None:
        return []
    fetch = opener if opener is not None else _default_opener
    return parse_symbols(fetch(url))


def members_in_universe(
    index_symbol: str,
    fno_symbols: Iterable[str],
    *,
    opener: Callable[[str], str] | None = None,
) -> list[str]:
    """Intersect an index's constituents with our F&O-eligible stocks.

    Fetches *index_symbol*'s constituents (best-effort) and keeps only
    those that appear in *fno_symbols* (the symbols we actually hold and
    can pull), matched **case-insensitively** and returned in the index's
    constituent order. Non-members are dropped silently — the universe is
    the source of truth for what is pullable.

    Parameters
    ----------
    index_symbol
        Engine index symbol.
    fno_symbols
        The F&O-eligible single-stock symbols we hold (any case).
    opener
        Injected fetcher; see :func:`fetch_constituent_symbols`.

    Returns
    -------
    list[str]
        The intersecting members, **in the original casing** of
        *fno_symbols* (so the dashboard ticks the exact rows it renders),
        order-preserved by the index constituent order. ``[]`` for an
        unknown index or when none of the constituents are F&O-eligible.
    """
    # Map upper-cased symbol → canonical (as-held) casing, first-seen wins.
    canonical: dict[str, str] = {}
    for sym in fno_symbols:
        key = sym.strip().upper()
        if key and key not in canonical:
            canonical[key] = sym
    members: list[str] = []
    for constituent in fetch_constituent_symbols(index_symbol, opener=opener):
        held = canonical.get(constituent.upper())
        if held is not None:
            members.append(held)
    return members


__all__ = [
    "INDEX_CSV_FILENAMES",
    "NSE_CONSTITUENTS_BASE_URL",
    "constituent_url",
    "fetch_constituent_symbols",
    "members_in_universe",
    "parse_symbols",
]
