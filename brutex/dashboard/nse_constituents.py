"""Best-effort NSE index → constituent-stock resolution (metadata helper).

DEC-UNIVERSE-NSE-CONSTITUENTS-001 (MIGRATION.md #445). Powers the
``/universe`` page "⊕ tick its constituents" action: when an INDEX is
selected, fetch its member stocks and auto-tick the ones that exist in
our F&O-eligible universe, ready for the unified "⤓ Pull spot".

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
import json
import os
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
    # Broad-market baskets (DEC-CONSTITUENTS-CACHE-001): NIFTY 100/200/500 are
    # NOT tradable index rows in the instrument store — they exist here ONLY as
    # constituent-basket selectors (pick the F&O-eligible stocks inside a wider
    # basket). Same NSE naming convention + host as the rows above; verify
    # against NSE (membership rebalances semi-annually, CLAUDE.md §7).
    "NIFTY 100": "ind_nifty100list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY 200": "ind_nifty200list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY 500": "ind_nifty500list.csv",
    "NIFTY500": "ind_nifty500list.csv",
    # NIFTY Total Market (~750 names: large+mid+small+micro cap) —
    # DEC-NIFTY-TOTAL-MARKET-001 + DEC-NIFTY-TOTAL-MARKET-URL-001 (MIGRATION.md).
    # A constituent-basket selector ONLY (like NIFTY 100/200/500 above); NOT a
    # tradable index row whose own OHLC we fetch. OPERATOR-VERIFIED full URL
    # (2026-06-06): this index's CSV lives on the NSE-Indices host
    # ``www.niftyindices.com/IndexConstituent/`` — a DIFFERENT host from the
    # ``nsearchives.nseindia.com`` base the bare-filename entries above use, so
    # it is stored as a FULL URL (used verbatim by ``constituent_url``, never
    # joined to the NSE base). Override env (BRUTEX_INDEX_CSV_FILENAMES) still
    # wins if NSE ever moves it.
    "NIFTY TOTAL MARKET": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "NIFTYTOTALMARKET": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "NIFTY_TOTAL_MARKET": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
}

#: Operator override env var (DEC-CONSTITUENTS-OVERRIDE-001): a JSON object
#: ``{index_symbol: csv_filename_or_full_url}`` MERGED over :data:`INDEX_CSV_FILENAMES`
#: so ANY index — the ~20 NSE sectorals, plus BSE SENSEX / BANKEX — becomes pickable
#: WITHOUT a code change. This build environment cannot reach NSE (it 403-bot-blocks
#: every request, verified), and per CLAUDE.md §1 (no hallucination) we will NOT
#: hardcode constituent-CSV filenames we cannot verify; the override lets the
#: operator — who CAN reach NSE/BSE — supply the filenames THEY have verified.
#: Value rules: a bare filename (e.g. ``"ind_niftyitlist.csv"``) resolves under the
#: NSE base URL (the built-in convention); a FULL ``http(s)://`` URL is used as-is
#: (so a BSE index points at the BSE host / any mirror). Operator entries WIN over
#: a built-in of the same key (so a renamed CSV can be corrected in place). Malformed
#: / absent → ignored (degrade to the built-ins, never crash). CLAUDE.md §11 DYNAMIC.
INDEX_CSV_FILENAMES_OVERRIDE_ENV: Final[str] = "BRUTEX_INDEX_CSV_FILENAMES"


def _override_filenames() -> dict[str, str]:
    """Parse the operator's JSON index→filename/URL override from the env — **pure**, never raises.

    Reads :data:`INDEX_CSV_FILENAMES_OVERRIDE_ENV`. Returns ``{}`` when absent,
    empty, not valid JSON, or not a JSON object; within a valid object, only
    ``str → non-blank str`` pairs are kept (keys upper-cased + trimmed, values
    trimmed). A lost / malformed override costs only the extra indices, never a
    crash (reliability) — exactly like the lake-sidecar degrade-to-empty rule.
    """
    raw = os.environ.get(INDEX_CSV_FILENAMES_OVERRIDE_ENV)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            out[key.strip().upper()] = value.strip()
    return out


@dataclass(frozen=True)
class ConstituentRow:
    """One NSE-index constituent: its ticker ``symbol`` + (optional) ``isin``.

    DEC-NIFTY-TOTAL-MARKET-MAP-001. The ISIN is the globally-unique join key
    used to map a constituent to its Dhan ``SECURITY_ID`` (the ticker alone is
    ambiguous — for non-F&O Dhan equities the engine ``symbol_name`` is the long
    company name, NOT the ticker, per DEC-DHAN-FNO-JOIN-KEY-001). ``isin`` is
    ``None`` only when the source CSV omits/blanks the ISIN column.
    """

    symbol: str
    isin: str | None


def parse_constituents(csv_text: str) -> list[ConstituentRow]:
    """Parse the ``Symbol`` + ``ISIN Code`` columns from an NSE constituent CSV.

    PURE — no I/O. The NSE constituent CSV columns are
    ``Company Name, Industry, Symbol, Series, ISIN Code``; this reads the
    ``Symbol`` and ``ISIN Code`` columns, strips whitespace, drops blank-symbol
    rows, and de-duplicates **by symbol, preserving first-seen order**. Header
    whitespace/case drift is tolerated (``" Symbol "``, ``"ISIN"`` vs
    ``"ISIN Code"``). Symbols carrying an ampersand (``M&M``, ``GVT&D``) survive
    intact (the CSV reader handles quoting). A missing/blank ISIN yields
    ``isin=None`` (the row is still returned — ticker-fallback may still map it).

    Parameters
    ----------
    csv_text
        The raw CSV text (a header row plus one row per constituent).

    Returns
    -------
    list[ConstituentRow]
        Constituents (symbol upper-cased + ISIN upper-cased or ``None``),
        de-duplicated by symbol, order-preserved. ``[]`` if the text is empty
        or has no ``Symbol`` column.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []
    # Tolerate header whitespace/case drift (e.g. " Symbol ", "ISIN" / "ISIN Code").
    symbol_key: str | None = None
    isin_key: str | None = None
    for name in reader.fieldnames:
        low = name.strip().lower()
        if low == "symbol":
            symbol_key = name
        elif low in ("isin code", "isin"):
            isin_key = name
    if symbol_key is None:
        return []
    seen: set[str] = set()
    rows: list[ConstituentRow] = []
    for row in reader:
        raw = row.get(symbol_key)
        if raw is None:
            continue
        sym = raw.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        isin_val: str | None = None
        if isin_key is not None:
            raw_isin = row.get(isin_key)
            if raw_isin is not None and raw_isin.strip():
                isin_val = raw_isin.strip().upper()
        rows.append(ConstituentRow(symbol=sym, isin=isin_val))
    return rows


def parse_symbols(csv_text: str) -> list[str]:
    """Parse just the ``Symbol`` column from an NSE constituent CSV.

    Thin wrapper over :func:`parse_constituents` (the single source of truth for
    constituent-CSV parsing — CLAUDE.md §11 COMMON). Returns the symbols only,
    upper-cased, de-duplicated, order-preserved; ``[]`` for empty / no-``Symbol``
    input.
    """
    return [row.symbol for row in parse_constituents(csv_text)]


def constituent_url(index_symbol: str) -> str | None:
    """Map an engine index symbol to its constituent-CSV URL.

    Case-insensitive lookup against the operator override
    (:func:`_override_filenames`, DEC-CONSTITUENTS-OVERRIDE-001) MERGED over the
    built-in :data:`INDEX_CSV_FILENAMES`, override winning. Each lookup is O(1)
    (two dict ``get``s). The resolved value is either a bare filename (joined to
    the NSE base URL — the built-in convention) or a full ``http(s)://`` URL used
    as-is (so an operator can point an index — e.g. BSE SENSEX — at any host).

    Returns
    -------
    str | None
        The full CSV URL, or ``None`` for an index with no built-in OR override
        mapping ("unknown-index").
    """
    key = index_symbol.strip().upper()
    value = _override_filenames().get(key) or INDEX_CSV_FILENAMES.get(key)
    if value is None:
        return None
    # A full URL (operator points an index at BSE / a mirror) is used verbatim; a
    # bare filename resolves under the NSE base (the built-in convention).
    if value.startswith(("http://", "https://")):
        return value
    return f"{NSE_CONSTITUENTS_BASE_URL}/{value}"


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
    # The S310 (ruff) + B310 (bandit) suppressions on the next line are honest:
    # the URL is a fixed https NSE host built by ``constituent_url`` (never
    # file:/ or a custom scheme); the whole opener is live-network-only glue
    # (``# pragma: no cover``).
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
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


def intersect_with_universe(
    constituents: Iterable[str],
    fno_symbols: Iterable[str],
) -> list[str]:
    """Keep only the *constituents* that are F&O-eligible stocks we hold.

    PURE — no I/O. The single source of truth for the constituent →
    universe intersection (CLAUDE.md §11 COMMON), shared by
    :func:`members_in_universe` (live fetch) and the cache-backed
    resolution path (:mod:`brutex.dashboard.constituents_cache`). Matches
    **case-insensitively** and returns the members in the **original
    casing** of *fno_symbols* (so the dashboard ticks the exact rows it
    renders), order-preserved by the *constituents* order. Non-members are
    dropped silently — the universe is the source of truth for what is
    pullable.

    Parameters
    ----------
    constituents
        The index's member symbols (any case), e.g. from a live NSE fetch
        or the on-disk cache.
    fno_symbols
        The F&O-eligible single-stock symbols we hold (any case).

    Returns
    -------
    list[str]
        The intersecting members in *fno_symbols*' casing, order-preserved
        by *constituents*. ``[]`` when none intersect.
    """
    # Map upper-cased symbol → canonical (as-held) casing, first-seen wins.
    canonical: dict[str, str] = {}
    for sym in fno_symbols:
        key = sym.strip().upper()
        if key and key not in canonical:
            canonical[key] = sym
    members: list[str] = []
    for constituent in constituents:
        held = canonical.get(constituent.strip().upper())
        if held is not None:
            members.append(held)
    return members


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
    return intersect_with_universe(
        fetch_constituent_symbols(index_symbol, opener=opener), fno_symbols
    )


__all__ = [
    "INDEX_CSV_FILENAMES",
    "INDEX_CSV_FILENAMES_OVERRIDE_ENV",
    "NSE_CONSTITUENTS_BASE_URL",
    "ConstituentRow",
    "constituent_url",
    "fetch_constituent_symbols",
    "intersect_with_universe",
    "members_in_universe",
    "parse_constituents",
    "parse_symbols",
]
