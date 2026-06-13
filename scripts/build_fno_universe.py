#!/usr/bin/env python3
"""Build a verified, timestamped F&O underlying universe for BruteX.

Self-contained (DEC-P0-026) — does NOT touch the live tickvault AWS; tickvault
is reference-only. Fetches three LIVE sources, cross-checks them, and writes
``data/fno_underlyings_<YYYYMMDD>.csv`` — failing loudly (non-zero exit, no
partial write) on any auth failure or short response (DEC-FNO-UNIVERSE-BUILDER-001).

SOURCES (fetched live, never hardcoded):
  1. NSE underlying-information API — the authoritative F&O underlying set:
     GET https://www.nseindia.com/api/underlying-information
  2. NSE lot-size CSV (cross-check + lot sizes):
     GET https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv
  3. Groww instrument master (underlying_security_id + segment flags) — reuses
     BruteX's own URL + parser (CLAUDE.md §11 COMMON).

NSE FETCH: bare requests 401/403, so we first GET https://www.nseindia.com/ to
obtain session cookies, persist the jar, THEN request the api/ + archives URLs
with those cookies + a browser User-Agent + Referer.

RESOLUTION RULES (match locked BruteX architecture):
  * the F&O underlyings are derived from the Groww master's derivative rows
    (FUT / CE / PE): each carries its ``underlying_symbol`` +
    ``underlying_security_id``, so the distinct underlyings are the canonical
    F&O-eligible spots (O(1) per row; no symbol string-matching).
  * a derivative whose underlying resolves to an index instrument is an index
    underlying (``OPTIDX``); otherwise a single-stock underlying (``OPTSTK``).

This script is the **network glue** (coverage-excluded — ``source = ["brutex"]``);
the deterministic transforms it calls live in ``brutex.data.fno_universe`` +
``brutex.adapters.groww.instruments`` and are 100%-tested.

Run on a machine with NSE access (e.g. the operator's Mac):
    python scripts/build_fno_universe.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.request import HTTPCookieProcessor, Request, build_opener

from brutex.adapters.groww.constants import GROWW_INSTRUMENT_CSV_URL
from brutex.adapters.groww.instruments import parse_instrument_csv
from brutex.data.fno_universe import (
    FnoUnderlying,
    common_symbols,
    cross_check,
    parse_fo_mktlots,
    to_csv,
)
from brutex.models import InstrumentType

_NSE_HOME = "https://www.nseindia.com/"
_NSE_UNDERLYING_API = "https://www.nseindia.com/api/underlying-information"
_FO_MKTLOTS = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
_FO_MKTLOTS_FALLBACK = "https://archives.nseindia.com/content/fo/fo_mktlots.csv"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
#: Loud-failure floors — a healthy F&O universe is ~200 stock underlyings.
_MIN_STOCK_UNDERLYINGS = 150
_TIMEOUT = 30


class BuildError(RuntimeError):
    """A source failed to fetch / parse, or returned implausibly few rows."""


def _opener() -> Any:
    return build_opener(HTTPCookieProcessor(CookieJar()))


def _get(opener: Any, url: str, *, referer: str | None = None) -> bytes:
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    with opener.open(Request(url, headers=headers), timeout=_TIMEOUT) as resp:  # noqa: S310 — hardcoded https NSE/Groww URLs only
        if resp.status != 200:
            raise BuildError(f"{url} -> HTTP {resp.status}")
        return resp.read()


def fetch_sources() -> tuple[dict[str, Any], str, str]:
    """Cookie-prime NSE then fetch (underlying-info JSON, fo_mktlots CSV, Groww CSV)."""
    opener = _opener()
    _get(opener, _NSE_HOME)  # prime session cookies
    api = json.loads(_get(opener, _NSE_UNDERLYING_API, referer=_NSE_HOME))
    try:
        mktlots = _get(opener, _FO_MKTLOTS, referer=_NSE_HOME).decode("utf-8")
    except OSError:
        mktlots = _get(opener, _FO_MKTLOTS_FALLBACK, referer=_NSE_HOME).decode("utf-8")
    groww = _get(_opener(), GROWW_INSTRUMENT_CSV_URL).decode("utf-8")
    return api, mktlots, groww


def nse_api_symbols(payload: dict[str, Any]) -> set[str]:
    """Symbols from the NSE underlying-information API (IndexList + UnderlyingList)."""
    out: set[str] = set()
    for key in ("IndexList", "UnderlyingList"):
        for entry in payload.get("data", payload).get(key, []) or []:
            sym = (entry.get("symbol") or entry.get("underlying") or "").strip()
            if sym:
                out.add(sym)
    return out


_DERIVATIVE_TYPES = frozenset({InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE})


def resolve_groww(groww_csv: str) -> list[FnoUnderlying]:
    """Resolve the Groww master to the canonical F&O-underlying spots (BruteX rules).

    The F&O underlyings are the distinct ``underlying_symbol`` referenced by the
    master's derivative rows (FUT / CE / PE), each carrying its
    ``underlying_security_id``. An underlying that itself resolves to an index
    spot (``IDX``) is an index underlying (``OPTIDX``); otherwise a single-stock
    underlying (``OPTSTK``). Reuses the 100%-tested Groww CSV parser.
    """
    pairs = parse_instrument_csv(groww_csv)
    index_symbols = {
        inst.symbol_name for _gsym, inst in pairs if inst.instrument is InstrumentType.IDX
    }
    # Distinct underlyings keyed on symbol; first derivative row wins for ids/exchange.
    by_symbol: dict[str, FnoUnderlying] = {}
    for _gsym, inst in pairs:
        if inst.instrument not in _DERIVATIVE_TYPES:
            continue
        underlying = inst.underlying_symbol
        if not underlying or inst.underlying_security_id is None or underlying in by_symbol:
            continue
        is_index = underlying in index_symbols
        by_symbol[underlying] = FnoUnderlying(
            symbol=underlying,
            underlying_security_id=inst.underlying_security_id,
            instrument_type="OPTIDX" if is_index else "OPTSTK",
            exchange=inst.exch_id.value,
            lot_size=0,  # filled from fo_mktlots below
        )
    return list(by_symbol.values())


def main() -> int:
    """Fetch → cross-check → write the timestamped universe CSV. Returns exit code."""
    api, mktlots_text, groww_csv = fetch_sources()

    mktlots = parse_fo_mktlots(mktlots_text)
    if len(mktlots.stock_lots) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError(f"fo_mktlots had only {len(mktlots.stock_lots)} stock underlyings")

    nse_syms = nse_api_symbols(api)
    if len(nse_syms) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError(f"NSE underlying-information returned only {len(nse_syms)} symbols")

    underlyings = resolve_groww(groww_csv)
    if len([u for u in underlyings if u.instrument_type == "OPTSTK"]) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError("Groww master resolved too few stock underlyings")

    # Stamp lot sizes from fo_mktlots; cross-check the three sources' symbol sets.
    lots = {**mktlots.index_lots, **mktlots.stock_lots}
    stamped = [
        FnoUnderlying(
            u.symbol, u.underlying_security_id, u.instrument_type, u.exchange, lots.get(u.symbol, 0)
        )
        for u in underlyings
    ]
    sources = {
        "nse_api": nse_syms,
        "fo_mktlots": set(mktlots.index_lots) | set(mktlots.stock_lots),
        "groww": {u.symbol for u in underlyings},
    }
    only_in = cross_check(sources)
    common = common_symbols(sources)

    fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"fno_underlyings_{datetime.now(UTC):%Y%m%d}.csv"
    text = to_csv(stamped, source="nse+groww", fetched_at_utc=fetched_at)
    # Atomic write — no partial file on failure.
    with tempfile.NamedTemporaryFile("w", dir=out_dir, delete=False, suffix=".tmp") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    tmp_path.replace(out_path)

    stocks = sum(1 for u in stamped if u.instrument_type == "OPTSTK")
    indices = len(stamped) - stocks
    print(f"✅ {out_path}: {len(stamped)} underlyings ({indices} indices, {stocks} stocks)")
    print(f"   common to all 3 sources: {len(common)}")
    for name, extra in only_in.items():
        if extra:
            print(f"   ⚠ only in {name} ({len(extra)}): {sorted(extra)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except (BuildError, OSError) as exc:
        print(f"❌ build failed: {exc}", file=sys.stderr)
        sys.exit(1)
