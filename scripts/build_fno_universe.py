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
  3. Dhan scrip master (UNDERLYING_SECURITY_ID + segment flags) — reuses
     BruteX's own URL + parser (CLAUDE.md §11 COMMON).

NSE FETCH: bare requests 401/403, so we first GET https://www.nseindia.com/ to
obtain session cookies, persist the jar, THEN request the api/ + archives URLs
with those cookies + a browser User-Agent + Referer.

RESOLUTION RULES (match locked BruteX architecture):
  * stock -> spot via the integer UNDERLYING_SECURITY_ID (one canonical spot,
    O(1)) — done inside ``select_universe_report``; no symbol string-matching.
  * exclude ``*NSETEST`` test scrips; stock F&O is NSE-only; indices keep all
    NSE indices + BSE SENSEX (the curated allow-list).

This script is the **network glue** (coverage-excluded — ``source = ["brutex"]``);
the deterministic transforms it calls live in ``brutex.data.fno_universe`` +
``brutex.adapters.dhan.instruments`` and are 100%-tested.

Run on a machine with NSE access (e.g. the operator's Mac):
    python scripts/build_fno_universe.py
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.request import HTTPCookieProcessor, Request, build_opener

from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instruments import select_universe_report
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
    with opener.open(Request(url, headers=headers), timeout=_TIMEOUT) as resp:  # noqa: S310 — hardcoded https NSE/Dhan URLs only
        if resp.status != 200:
            raise BuildError(f"{url} -> HTTP {resp.status}")
        return resp.read()


def fetch_sources() -> tuple[dict[str, Any], str, str]:
    """Cookie-prime NSE then fetch (underlying-info JSON, fo_mktlots CSV, Dhan CSV)."""
    opener = _opener()
    _get(opener, _NSE_HOME)  # prime session cookies
    api = json.loads(_get(opener, _NSE_UNDERLYING_API, referer=_NSE_HOME))
    try:
        mktlots = _get(opener, _FO_MKTLOTS, referer=_NSE_HOME).decode("utf-8")
    except OSError:
        mktlots = _get(opener, _FO_MKTLOTS_FALLBACK, referer=_NSE_HOME).decode("utf-8")
    dhan = _get(_opener(), INSTRUMENT_MASTER_DETAILED_URL).decode("utf-8")
    return api, mktlots, dhan


def nse_api_symbols(payload: dict[str, Any]) -> set[str]:
    """Symbols from the NSE underlying-information API (IndexList + UnderlyingList)."""
    out: set[str] = set()
    for key in ("IndexList", "UnderlyingList"):
        for entry in payload.get("data", payload).get(key, []) or []:
            sym = (entry.get("symbol") or entry.get("underlying") or "").strip()
            if sym:
                out.add(sym)
    return out


def resolve_dhan(dhan_csv: str) -> list[FnoUnderlying]:
    """Resolve the Dhan master to the canonical F&O-underlying spots (BruteX rules)."""
    rows = list(csv.DictReader(io.StringIO(dhan_csv)))
    report = select_universe_report(rows)
    out: list[FnoUnderlying] = []
    for inst in report.universe:
        is_index = inst.instrument_type is InstrumentType.IDX
        out.append(
            FnoUnderlying(
                symbol=inst.trading_symbol,
                underlying_security_id=inst.exchange_token,
                instrument_type="OPTIDX" if is_index else "OPTSTK",
                exchange=inst.exchange.value,
                lot_size=0,  # filled from fo_mktlots below
            )
        )
    return out


def main() -> int:
    """Fetch → cross-check → write the timestamped universe CSV. Returns exit code."""
    api, mktlots_text, dhan_csv = fetch_sources()

    mktlots = parse_fo_mktlots(mktlots_text)
    if len(mktlots.stock_lots) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError(f"fo_mktlots had only {len(mktlots.stock_lots)} stock underlyings")

    nse_syms = nse_api_symbols(api)
    if len(nse_syms) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError(f"NSE underlying-information returned only {len(nse_syms)} symbols")

    underlyings = resolve_dhan(dhan_csv)
    if len([u for u in underlyings if u.instrument_type == "OPTSTK"]) < _MIN_STOCK_UNDERLYINGS:
        raise BuildError("Dhan master resolved too few stock underlyings")

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
        "dhan": {u.symbol for u in underlyings},
    }
    only_in = cross_check(sources)
    common = common_symbols(sources)

    fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"fno_underlyings_{datetime.now(UTC):%Y%m%d}.csv"
    text = to_csv(stamped, source="nse+dhan", fetched_at_utc=fetched_at)
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
