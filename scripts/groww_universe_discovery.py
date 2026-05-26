#!/usr/bin/env python3
"""Groww instrument universe discovery (DEC-VENDOR-PIVOT-001 Phase 0c).

STANDALONE script — imports ZERO ``brutex/`` modules. Companion to
``scripts/groww_probe.py`` (Phase 0b). Downloads Groww's instruments CSV
and produces 4 structured JSON inventories that enumerate the entire
NSE + BSE universe available via the Groww Trade API:

* ``01_all_nse_indices.json``       — every NSE index (sectoral + broad-market + volatility)
* ``02_all_bse_indices.json``       — every BSE index
* ``03_fno_eligible_stocks.json``   — single stocks with futures + options listed
* ``04_fno_eligible_indices.json``  — indices with futures + options listed
* ``05_universe_summary.json``      — totals + cross-stats

These outputs answer the operator's "how do we know the COMPLETE universe?"
question (per ``docs/groww/api-reference/02-instruments.md``):

* All NSE sectoral indices: NIFTY AUTO, NIFTY REALTY, NIFTY IT, NIFTY METAL,
  NIFTY PHARMA, NIFTY FMCG, NIFTY ENERGY, NIFTY MEDIA, NIFTY PSU BANK,
  NIFTY PVT BANK, NIFTY OIL & GAS, NIFTY HEALTHCARE, NIFTY FIN SERVICE, etc.
* All NSE broad-market indices: NIFTY 50, NIFTY 100, NIFTY 200, NIFTY 500,
  NIFTY MIDCAP 100, NIFTY SMALLCAP 100, NIFTY NEXT 50, etc.
* NSE volatility: INDIA VIX.
* F&O eligible stocks (NSE's official "F&O securities" list, ~200 stocks).
* F&O eligible indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, BANKEX, etc.

USAGE
=====

This script does NOT require Groww auth — it only downloads the public
instruments CSV at ``https://growwapi-assets.groww.in/instruments/instrument.csv``.
Anyone can run it.

::

    python scripts/groww_universe_discovery.py

Outputs land in ``docs/groww/universe/``.

EXIT CODES
==========

* 0 — CSV downloaded and 5 JSON files written successfully
* 2 — CSV download failed (network / 403 / etc.)
* 3 — CSV parse failed (schema drift)

DECISION ID
===========

DEC-VENDOR-PIVOT-001 Phase 0c (companion to Phase 0b probe). Builds on
``docs/groww/api-reference/02-instruments.md`` schema (17 columns) and
``docs/TRACK1_GROWW_ADAPTER.md §3.2`` LAKE-only universe (250 instruments).
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "docs" / "groww" / "universe"

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"

# ─────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────


@dataclass
class IndexRow:
    """One row from the indices inventory."""

    exchange: str
    trading_symbol: str
    groww_symbol: str
    name: str
    has_fno_listing: bool  # True if futures or options exist on this index


@dataclass
class StockRow:
    """One row from the F&O-eligible stocks inventory."""

    exchange: str
    trading_symbol: str
    groww_symbol: str
    isin: str
    lot_size: int
    futures_listed: bool
    options_listed: bool
    nearest_expiry_dates: list[str] = field(default_factory=list)


@dataclass
class FnoIndexRow:
    """One row from the F&O-eligible indices inventory."""

    exchange: str
    trading_symbol: str
    groww_symbol: str
    name: str
    futures_listed: bool
    options_listed: bool
    nearest_expiry_dates: list[str] = field(default_factory=list)


@dataclass
class UniverseSummary:
    """Cross-stats over the full universe."""

    fetched_at: str
    csv_url: str
    csv_byte_count: int
    total_csv_rows: int
    total_nse_indices: int
    total_bse_indices: int
    total_fno_stocks: int
    total_fno_indices: int
    total_eq_stocks_in_cash: int
    total_fut_contracts: int
    total_ce_contracts: int
    total_pe_contracts: int
    distinct_expiry_dates: int


# ─────────────────────────────────────────────────────────────────
# CSV download + parse
# ─────────────────────────────────────────────────────────────────


def _download_csv() -> str:
    """Download instruments CSV. Exits 2 on failure."""
    print(f"  Downloading {INSTRUMENT_CSV_URL}...")
    try:
        request = urllib.request.Request(  # noqa: S310 — Groww-owned asset URL
            INSTRUMENT_CSV_URL,
            headers={"User-Agent": "BruteX-UniverseDiscovery/1.0"},
        )
        with urllib.request.urlopen(request, timeout=60.0) as response:  # noqa: S310
            text = response.read().decode("utf-8")
        print(f"  Downloaded {len(text)} bytes.")
        return text
    except Exception as exc:
        print(f"❌ CSV download failed: {exc}", file=sys.stderr)
        sys.exit(2)


def _parse_csv(text: str) -> list[dict[str, str]]:
    """Parse CSV text into list of row dicts. Exits 3 on schema drift."""
    expected_columns = {
        "exchange",
        "exchange_token",
        "trading_symbol",
        "groww_symbol",
        "name",
        "instrument_type",
        "segment",
    }
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        print("❌ CSV has zero rows.", file=sys.stderr)
        sys.exit(3)
    actual_columns = set(rows[0].keys())
    missing = expected_columns - actual_columns
    if missing:
        print(f"❌ CSV schema drift — missing columns: {sorted(missing)}", file=sys.stderr)
        print(f"   Actual columns: {sorted(actual_columns)}", file=sys.stderr)
        sys.exit(3)
    print(f"  Parsed {len(rows)} rows with {len(actual_columns)} columns.")
    return rows


# ─────────────────────────────────────────────────────────────────
# Discovery functions
# ─────────────────────────────────────────────────────────────────


def discover_indices(rows: list[dict[str, str]], exchange: str) -> list[IndexRow]:
    """Find all indices on the given exchange (sectoral + broad-market + volatility)."""
    # Get unique underlying_symbols for FNO-listed rows on this exchange
    fno_underlyings = {
        row["underlying_symbol"]
        for row in rows
        if row["instrument_type"] in ("FUT", "CE", "PE")
        and row["exchange"] == exchange
        and row.get("underlying_symbol")
    }

    index_rows: list[IndexRow] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row["instrument_type"] != "IDX":
            continue
        if row["exchange"] != exchange:
            continue
        key = (row["exchange"], row["trading_symbol"])
        if key in seen:
            continue
        seen.add(key)
        index_rows.append(
            IndexRow(
                exchange=row["exchange"],
                trading_symbol=row["trading_symbol"],
                groww_symbol=row["groww_symbol"],
                name=row.get("name") or "",
                has_fno_listing=row["trading_symbol"] in fno_underlyings,
            )
        )
    index_rows.sort(key=lambda r: r.trading_symbol)
    return index_rows


def discover_fno_stocks(rows: list[dict[str, str]]) -> list[StockRow]:
    """Find all F&O-eligible single stocks (underlying exists as EQ in CASH)."""
    # Build EQ stock lookup
    eq_stocks: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row["instrument_type"] == "EQ" and row["segment"] == "CASH":
            eq_stocks[(row["exchange"], row["trading_symbol"])] = row

    # Find unique underlyings in FNO rows
    fno_underlyings_meta: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "futures_listed": False,
            "options_listed": False,
            "expiries": set(),
        }
    )
    for row in rows:
        if row["instrument_type"] not in ("FUT", "CE", "PE"):
            continue
        underlying = row.get("underlying_symbol", "")
        if not underlying:
            continue
        key = (row["exchange"], underlying)
        if row["instrument_type"] == "FUT":
            fno_underlyings_meta[key]["futures_listed"] = True
        else:  # CE / PE
            fno_underlyings_meta[key]["options_listed"] = True
        expiry = row.get("expiry_date", "")
        if expiry:
            fno_underlyings_meta[key]["expiries"].add(expiry)

    stock_rows: list[StockRow] = []
    for (exchange, sym), meta in fno_underlyings_meta.items():
        eq = eq_stocks.get((exchange, sym))
        if eq is None:
            continue  # not a stock — likely an index
        try:
            lot_size = int(float(eq.get("lot_size") or "0"))
        except (ValueError, TypeError):
            lot_size = 0
        sorted_expiries = sorted(meta["expiries"])
        stock_rows.append(
            StockRow(
                exchange=exchange,
                trading_symbol=sym,
                groww_symbol=eq["groww_symbol"],
                isin=eq.get("isin") or "",
                lot_size=lot_size,
                futures_listed=meta["futures_listed"],
                options_listed=meta["options_listed"],
                nearest_expiry_dates=sorted_expiries[:5],
            )
        )
    stock_rows.sort(key=lambda r: (r.exchange, r.trading_symbol))
    return stock_rows


def discover_fno_indices(rows: list[dict[str, str]]) -> list[FnoIndexRow]:
    """Find all F&O-eligible indices (underlying exists as IDX)."""
    idx_rows: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if row["instrument_type"] == "IDX":
            idx_rows[(row["exchange"], row["trading_symbol"])] = row

    fno_underlyings_meta: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "futures_listed": False,
            "options_listed": False,
            "expiries": set(),
        }
    )
    for row in rows:
        if row["instrument_type"] not in ("FUT", "CE", "PE"):
            continue
        underlying = row.get("underlying_symbol", "")
        if not underlying:
            continue
        key = (row["exchange"], underlying)
        if row["instrument_type"] == "FUT":
            fno_underlyings_meta[key]["futures_listed"] = True
        else:
            fno_underlyings_meta[key]["options_listed"] = True
        expiry = row.get("expiry_date", "")
        if expiry:
            fno_underlyings_meta[key]["expiries"].add(expiry)

    fno_idx_rows: list[FnoIndexRow] = []
    for (exchange, sym), meta in fno_underlyings_meta.items():
        idx = idx_rows.get((exchange, sym))
        if idx is None:
            continue  # not an index
        sorted_expiries = sorted(meta["expiries"])
        fno_idx_rows.append(
            FnoIndexRow(
                exchange=exchange,
                trading_symbol=sym,
                groww_symbol=idx["groww_symbol"],
                name=idx.get("name") or "",
                futures_listed=meta["futures_listed"],
                options_listed=meta["options_listed"],
                nearest_expiry_dates=sorted_expiries[:5],
            )
        )
    fno_idx_rows.sort(key=lambda r: (r.exchange, r.trading_symbol))
    return fno_idx_rows


def build_summary(rows: list[dict[str, str]], csv_text: str) -> UniverseSummary:
    """Cross-stat totals from the parsed CSV."""
    nse_indices = sum(1 for r in rows if r["instrument_type"] == "IDX" and r["exchange"] == "NSE")
    bse_indices = sum(1 for r in rows if r["instrument_type"] == "IDX" and r["exchange"] == "BSE")
    eq_stocks = sum(1 for r in rows if r["instrument_type"] == "EQ" and r["segment"] == "CASH")
    fut_contracts = sum(1 for r in rows if r["instrument_type"] == "FUT")
    ce_contracts = sum(1 for r in rows if r["instrument_type"] == "CE")
    pe_contracts = sum(1 for r in rows if r["instrument_type"] == "PE")

    fno_eligible_underlyings = {
        (r["exchange"], r.get("underlying_symbol", ""))
        for r in rows
        if r["instrument_type"] in ("FUT", "CE", "PE") and r.get("underlying_symbol")
    }
    eq_lookup = {
        (r["exchange"], r["trading_symbol"])
        for r in rows
        if r["instrument_type"] == "EQ" and r["segment"] == "CASH"
    }
    idx_lookup = {
        (r["exchange"], r["trading_symbol"]) for r in rows if r["instrument_type"] == "IDX"
    }
    fno_stocks = sum(1 for k in fno_eligible_underlyings if k in eq_lookup)
    fno_indices = sum(1 for k in fno_eligible_underlyings if k in idx_lookup)

    distinct_expiries = len({r.get("expiry_date", "") for r in rows if r.get("expiry_date")})

    return UniverseSummary(
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        csv_url=INSTRUMENT_CSV_URL,
        csv_byte_count=len(csv_text),
        total_csv_rows=len(rows),
        total_nse_indices=nse_indices,
        total_bse_indices=bse_indices,
        total_fno_stocks=fno_stocks,
        total_fno_indices=fno_indices,
        total_eq_stocks_in_cash=eq_stocks,
        total_fut_contracts=fut_contracts,
        total_ce_contracts=ce_contracts,
        total_pe_contracts=pe_contracts,
        distinct_expiry_dates=distinct_expiries,
    )


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────


def main() -> int:
    """Run universe discovery; persist 5 JSON inventories to OUTPUT_DIR."""
    print("=" * 65)
    print("BruteX Phase 0c — Groww instrument universe discovery")
    print("(DEC-VENDOR-PIVOT-001 companion to Phase 0b probe)")
    print("=" * 65)
    print()
    print("[1/3] Downloading + parsing instrument.csv...")
    csv_text = _download_csv()
    rows = _parse_csv(csv_text)

    print("\n[2/3] Discovering universe categories...")
    nse_indices = discover_indices(rows, exchange="NSE")
    print(f"  NSE indices: {len(nse_indices)} found")
    bse_indices = discover_indices(rows, exchange="BSE")
    print(f"  BSE indices: {len(bse_indices)} found")
    fno_stocks = discover_fno_stocks(rows)
    print(f"  F&O eligible stocks: {len(fno_stocks)} found")
    fno_indices = discover_fno_indices(rows)
    print(f"  F&O eligible indices: {len(fno_indices)} found")

    summary = build_summary(rows, csv_text)
    print(f"  Total CSV rows: {summary.total_csv_rows}")
    print(f"  Distinct expiry dates: {summary.distinct_expiry_dates}")

    print("\n[3/3] Writing 5 JSON files...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _write_json("01_all_nse_indices.json", [asdict(i) for i in nse_indices])
    _write_json("02_all_bse_indices.json", [asdict(i) for i in bse_indices])
    _write_json("03_fno_eligible_stocks.json", [asdict(s) for s in fno_stocks])
    _write_json("04_fno_eligible_indices.json", [asdict(i) for i in fno_indices])
    _write_json("05_universe_summary.json", asdict(summary))

    print()
    print("=" * 65)
    print("Universe discovery complete. Inventories in:")
    print(f"   {OUTPUT_DIR}")
    print("=" * 65)
    print()
    print("Sample of NSE indices found (first 15):")
    for idx in nse_indices[:15]:
        fno_marker = " 🟢 F&O" if idx.has_fno_listing else ""
        print(f"   {idx.exchange}-{idx.trading_symbol:24s}  {idx.groww_symbol}{fno_marker}")

    return 0


def _write_json(filename: str, payload: Any) -> None:
    """Write payload as pretty JSON to OUTPUT_DIR/filename."""
    path = OUTPUT_DIR / filename
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    size_kb = path.stat().st_size / 1024.0
    print(f"  Saved: {path.relative_to(REPO_ROOT)} ({size_kb:.1f} KB)")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
