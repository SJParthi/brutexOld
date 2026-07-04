"""Best-effort NSE index → constituent-stock resolution (metadata helper).

DEC-UNIVERSE-NSE-CONSTITUENTS-001 (MIGRATION.md #445). Powers the
``/universe`` page "⊕ tick its constituents" action: when an INDEX is
selected, fetch its member stocks and auto-tick the ones that exist in
our F&O-eligible universe, ready for the unified "⤓ Pull spot".

## Honesty (CLAUDE.md §1 no-hallucination, §10 honest limits)

This is **NOT vendor data** — the Groww instrument master does not carry
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
import re
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

import structlog

_LOG = structlog.get_logger(__name__)

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
    # ABBREVIATED ("MKT") aliases — DEC-NIFTY-TOTAL-MARKET-MKT-ALIAS-001: the Groww
    # detailed-master spells this index "NIFTY TOTAL MKT" (the value
    # ``ntm.NTM_SYMBOL`` holds + the display symbol the /universe page sends to
    # ``/api/v1/universe/index-constituents``), NOT the niftyindices "MARKET"
    # spelling. Without these the "⊕ tick its constituents" button resolved to
    # "unknown-index" for the very index BruteX admits with ~750 constituents
    # (operator make-run 2026-06-06: ``ntm_admitted constituents=750`` yet the UI
    # showed "No NSE constituent list known"). All spellings map to the same
    # operator-verified CSV (``ntm.NTM_NAME_PATTERN`` already matches MKT|MARKET).
    "NIFTY TOTAL MKT": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "NIFTYTOTALMKT": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "NIFTY_TOTAL_MKT": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    # DEC-INDEX-CONSTITUENTS-COVERAGE-001: the Groww instrument master spells the
    # NIFTY Total Market index ``NIFTYTOTALMCAP`` (engine ``symbol_name``; display
    # name "Nifty Total Market" — OPERATOR-VERIFIED on the live ``/universe`` row,
    # 2026-06-13). Without this alias the "⊕ tick its constituents" button resolved
    # to "unknown-index" for the very index BruteX admits with ~750 constituents
    # (``ntm_admitted constituents=755``). Same operator-verified niftyindices CSV
    # as the MARKET/MKT spellings above; the equivalence is traceable to the
    # vendor master row, NOT hardcoded membership (CLAUDE.md §1).
    "NIFTYTOTALMCAP": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "NIFTY_TOTAL_MCAP": "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
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
    used to map a constituent to its vendor ``SECURITY_ID`` (the ticker alone is
    ambiguous — for non-F&O vendor equities the engine ``symbol_name`` is the long
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


def convention_constituent_url(index_symbol: str) -> str | None:
    """Derive a BEST-EFFORT NSE constituent-CSV URL by NSE's PUBLIC filename convention.

    NSE publishes per-index constituent CSVs as ``ind_<index>list.csv`` under
    :data:`NSE_CONSTITUENTS_BASE_URL` (``nseindia.com`` — e.g. the verified
    ``ind_nifty50list.csv`` / ``ind_niftybanklist.csv`` / ``ind_niftypharmalist.csv``
    entries in :data:`INDEX_CSV_FILENAMES`). This derives that candidate for an
    index that has no explicit/override mapping: the engine symbol lower-cased,
    stripped to ASCII letters+digits, wrapped as ``ind_<safe>list.csv`` (so
    ``NIFTYPHARMA`` → ``…/ind_niftypharmalist.csv``).

    HONESTY (CLAUDE.md §1): this is a SELF-VALIDATING candidate, never an assertion
    that NSE serves the file — the caller fetches it and a 404 / empty body simply
    yields **no data** (the route then shows its honest "unavailable" note), never
    WRONG data. The actual constituent symbols always come from NSE's response, so
    nothing is fabricated. Returns ``None`` when the symbol has no alphanumeric
    characters (no filename can be formed) — the only remaining "unknown-index".
    """
    safe = re.sub(r"[^a-z0-9]", "", index_symbol.strip().lower())
    if not safe:
        return None
    return f"{NSE_CONSTITUENTS_BASE_URL}/ind_{safe}list.csv"


#: Live-fetch tuning — BOUNDED + env-tunable (CLAUDE.md §11 DYNAMIC). This live path
#: is reached ONLY on a cold cache (first-ever boot) or a post-TTL refresh: the
#: steady-state ``/universe`` load is CACHE-FIRST
#: (``instrument_loader._resolve_constituent_csv``, DEC-NTM-CACHE-FIRST-001) and
#: serves a fresh cache with NO network call at all, so these defaults govern only
#: the rare refresh — never the hot path, and they are paid AT MOST ONCE per TTL,
#: not on every load as before.
#:
#: The per-attempt timeout DEFAULT stays the operator-validated **30 s** — the prior
#: code's comment recorded that a single 10 s attempt read-TIMED-OUT for the operator
#: on this exact host and 30 s was the deliberate fix — so the headline win here is the
#: cache-first ORDERING (the live fetch now happens at most once per TTL), NOT a
#: timeout cut: keeping 30 s maximises the chance that the ONE cold fetch SUCCEEDS and
#: seeds the cache → instant forever (a cut-and-FAILED fetch would crater a cold boot
#: to the ~211 F&O base set — the worse outcome). Retries are trimmed 3 → 2, so the
#: worst case is **30 s × 2 ≈ 61.5 s** (down from the old 30 s × 3 ≈ 90 s) for that
#: single cold fetch. An operator who prefers a HARDER fail-fast lowers
#: ``BRUTEX_NSE_FETCH_TIMEOUT_SECONDS`` / ``BRUTEX_NSE_FETCH_MAX_RETRIES`` (accepting a
#: higher chance the cold fetch fails → base set until the cache is seeded).
_FETCH_TIMEOUT_ENV: Final[str] = "BRUTEX_NSE_FETCH_TIMEOUT_SECONDS"
_FETCH_RETRIES_ENV: Final[str] = "BRUTEX_NSE_FETCH_MAX_RETRIES"
_FETCH_TIMEOUT_DEFAULT: Final[float] = 30.0
_FETCH_RETRIES_DEFAULT: Final[int] = 2


def _fetch_timeout_seconds() -> float:
    """Per-attempt read timeout (seconds) — env-tunable, malformed/≤0 → default. PURE.

    Reads :data:`_FETCH_TIMEOUT_ENV`. Absent, non-numeric, or a non-positive value all
    fall back to :data:`_FETCH_TIMEOUT_DEFAULT` (a lost/garbled override costs only the
    tuning, never a crash — reliability). 100% unit-tested; the opener that USES it is
    the ``# pragma: no cover`` live surface.
    """
    raw = os.environ.get(_FETCH_TIMEOUT_ENV)
    if not raw:
        return _FETCH_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _FETCH_TIMEOUT_DEFAULT
    return value if value > 0.0 else _FETCH_TIMEOUT_DEFAULT


def _fetch_max_retries() -> int:
    """Bounded attempt count — env-tunable, malformed/<1 → default. PURE.

    Reads :data:`_FETCH_RETRIES_ENV`. Absent, non-integer, or a value below 1 all fall
    back to :data:`_FETCH_RETRIES_DEFAULT` (so the live path always makes at least one
    attempt). 100% unit-tested.
    """
    raw = os.environ.get(_FETCH_RETRIES_ENV)
    if not raw:
        return _FETCH_RETRIES_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _FETCH_RETRIES_DEFAULT
    return value if value >= 1 else _FETCH_RETRIES_DEFAULT


#: SSRF allowlist for the live constituent-CSV fetch — DEFENSE-IN-DEPTH, the
#: constituent twin of the instrument-master ``_check_url`` guard
#: (``brutex/adapters/groww/instrument_loader.py``) and the bhavcopy
#: ``BHAVCOPY_ALLOWED_HOSTS`` discipline. Exactly the hosts the built-in
#: defaults resolve to: the :data:`NSE_CONSTITUENTS_BASE_URL` default host
#: (``nsearchives.nseindia.com``) + the operator-verified NIFTY Total Market
#: host (``www.niftyindices.com``, plus its bare apex). DELIBERATELY FIXED —
#: not env-extensible: an env-override URL (the ``BRUTEX_NSE_CONSTITUENTS_BASE_URL``
#: base or a ``BRUTEX_INDEX_CSV_FILENAMES`` full URL) must STILL land on one of
#: these hosts over ``https``, or :func:`_default_opener` refuses LOUDLY
#: (a repointed mirror off these hosts needs a code change + review, never a
#: silent env flip — the SSRF trap this closes).
NSE_CONSTITUENT_ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "nsearchives.nseindia.com",
        "niftyindices.com",
        "www.niftyindices.com",
    }
)


def _check_constituent_url(url: str) -> None:
    """Refuse any constituent-CSV URL that is not ``https`` on an allowlisted host.

    INDEPENDENT guard inside the live opener (defense-in-depth — it re-validates
    whatever URL resolution produced, so neither an env-override base/full URL
    nor a code drift can point the real network fetch off the NSE/niftyindices
    hosts, mirroring the instrument-master ``_check_url``). PURE + 100% tested;
    raises a clear :class:`ValueError` (logged via structlog) on any violation.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in NSE_CONSTITUENT_ALLOWED_HOSTS:
        _LOG.warning(
            "nse_constituents.url_rejected",
            url=url,
            scheme=parsed.scheme,
            host=parsed.hostname,
            allowed_hosts=sorted(NSE_CONSTITUENT_ALLOWED_HOSTS),
        )
        raise ValueError(
            f"refusing non-allowlisted constituent-CSV URL {url!r} — the live fetch "
            f"requires https on one of {sorted(NSE_CONSTITUENT_ALLOWED_HOSTS)} "
            "(an env-override host must still be on this fixed allowlist)"
        )


def _default_opener(url: str) -> str:
    """Fetch *url* and return its text body (the real urllib opener, SSRF-guarded).

    The allowlist check (:func:`_check_constituent_url`) runs FIRST — an
    ``http://`` URL or a non-allowlisted host raises :class:`ValueError` before
    any network I/O. The network body itself lives in :func:`_open_live_url`
    (``# pragma: no cover`` — live only). Callers inject a fake ``opener`` in
    tests; this default is reached ONLY on a cold cache / post-TTL refresh.
    """
    _check_constituent_url(url)
    return _open_live_url(url)


def _open_live_url(url: str) -> str:  # pragma: no cover - live network only
    """The raw urllib fetch behind :func:`_default_opener` (already allowlist-checked).

    LIVE ONLY (``# pragma: no cover``): NSE bot-blocks these endpoints
    and this build environment network-blocks ``nseindia.com``, so this
    cannot be exercised in CI. A browser-like ``User-Agent`` + ``Accept``
    + a BOUNDED, env-tunable timeout + a few bounded retries give the
    best-effort fetch its best chance on the operator's machine
    (niftyindices.com is SLOW to first-byte for non-browser clients).
    Reached ONLY on a cold cache / post-TTL refresh — the steady-state
    load is cache-first and never calls this. Callers inject a fake
    ``opener`` in tests; the bound + retries come from the
    100%-tested pure helpers :func:`_fetch_timeout_seconds` /
    :func:`_fetch_max_retries`.
    """
    import time

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
            "Referer": "https://www.niftyindices.com/",
        },
    )
    # The S310 (ruff) + B310 (bandit) suppressions are honest: the URL has been
    # ENFORCED https on an allowlisted NSE/niftyindices host by
    # ``_check_constituent_url`` in ``_default_opener`` before this body runs
    # (never file:/ or a custom scheme); the network body is live-only glue
    # (``# pragma: no cover``).
    # Bounded retry (env-tunable attempts, env-tunable read timeout, short backoff)
    # so a slow first-byte / transient throttle recovers instead of cratering the
    # universe — but bounded far below the old fixed 90 s.
    timeout = _fetch_timeout_seconds()
    max_retries = _fetch_max_retries()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
                body: bytes = response.read()
            return body.decode("utf-8")
        except OSError as exc:  # timeout / transport — retry the bounded budget
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc is not None else OSError("constituent fetch failed")


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


def fetch_constituent_csv(
    index_symbol: str,
    *,
    opener: Callable[[str], str] | None = None,
) -> str | None:
    """Resolve *index_symbol* → URL → fetched RAW CSV text (Symbol + ISIN Code).

    The text sibling of :func:`fetch_constituent_symbols`: returns the full CSV
    body so callers that need the **ISIN** column (e.g. NTM constituent →
    ``SECURITY_ID`` resolution against the vendor master, DEC-NIFTY-TOTAL-MARKET-MAP-001)
    can parse it themselves, not just the symbol list. Returns ``None`` for an
    unknown index (no URL mapping). Fetch errors from *opener* PROPAGATE (the
    caller decides whether to degrade). Tests inject a fake *opener* offline.
    """
    url = constituent_url(index_symbol)
    if url is None:
        return None
    fetch = opener if opener is not None else _default_opener
    return fetch(url)


def fetch_symbols_at_url(url: str, *, opener: Callable[[str], str] | None = None) -> list[str]:
    """Fetch + parse the constituent symbols at an EXACT *url* (no symbol re-derivation).

    The direct-URL sibling of :func:`fetch_constituent_symbols`: callers that
    resolve a URL themselves (e.g. the convention fallback in
    :func:`~brutex.dashboard.constituents_cache.resolve_constituents`, which must
    fetch :func:`convention_constituent_url` — a URL ``constituent_url`` would not
    re-derive) pass it here. *opener* defaults to the real urllib opener
    (:func:`_default_opener`, ``# pragma: no cover`` live surface); tests inject a
    fake. Fetch errors PROPAGATE (the caller maps them to the honest degrade).
    """
    fetch = opener if opener is not None else _default_opener
    return parse_symbols(fetch(url))


def fetch_rows_at_url(
    url: str, *, opener: Callable[[str], str] | None = None
) -> list[ConstituentRow]:
    """Fetch + parse the constituent ROWS (symbol + ISIN) at an EXACT *url*.

    The ISIN-carrying sibling of :func:`fetch_symbols_at_url`: returns each
    member's ``(symbol, isin)`` so the caller can fall back to the globally-unique
    ISIN when a ticker has been renamed (rename-proof matching,
    DEC-INDEX-CONSTITUENTS-NSE-001). Used by the cache layer's row resolution.
    *opener* defaults to the live urllib opener (:func:`_default_opener`,
    ``# pragma: no cover`` live surface); tests inject a fake. Fetch errors
    PROPAGATE (the caller maps them to the honest degrade).
    """
    fetch = opener if opener is not None else _default_opener
    return parse_constituents(fetch(url))


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
    "NSE_CONSTITUENT_ALLOWED_HOSTS",
    "ConstituentRow",
    "constituent_url",
    "convention_constituent_url",
    "fetch_constituent_csv",
    "fetch_constituent_symbols",
    "fetch_rows_at_url",
    "fetch_symbols_at_url",
    "intersect_with_universe",
    "members_in_universe",
    "parse_constituents",
    "parse_symbols",
]
