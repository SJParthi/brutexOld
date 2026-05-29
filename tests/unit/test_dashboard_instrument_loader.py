"""Tests for ``brutex.dashboard.instrument_loader`` — keyless instrument load.

Injects a fake ``http_get`` returning a small valid instrument CSV against a
REAL temp :class:`~brutex.data.instrument_store.InstrumentStore` and asserts
the loader parses/filters correctly, re-summarises the stored snapshot into
``index_count`` / ``stock_count``, aborts on a malformed header, and handles
an empty CSV. NO Groww auth / creds anywhere — the master is a public CSV.

CSV patterns follow ``tests/unit/test_groww_instruments.py``; the real store
usage follows ``tests/integration/test_universe_explorer_store.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore
from brutex.models import InstrumentFormatError

_HEADER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,"
    "series,isin,underlying_symbol,underlying_exchange_token,expiry_date,strike_price,"
    "lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed"
)
# One NSE index (IDX), one F&O-eligible stock (RELIANCE EQ) + a RELIANCE
# derivative (CE) that makes the stock F&O-eligible, plus one out-of-scope
# MCX row that must be filtered (not parsed). The second IDX row
# (BANKNIFTY) is REAL-STYLE per DEC-INDEX-PARSE-FIX-001 — BLANK lot_size /
# tick_size and a non-numeric exchange_token (the NSE index SYMBOL string).
# Before the fix it quarantined (index_count was 1); now it parses, so the
# expected index_count is 2.
_ROWS = [
    "NSE,26000,NIFTY,NSE-NIFTY,Nifty 50,IDX,CASH,NaN,NaN,NaN,NaN,NaN,NaN,1,0.05,NaN,0,1,1",
    "NSE,BANKNIFTY,BANKNIFTY,NSE-BANKNIFTY,NIFTY Bank,IDX,CASH,,,,,,,,,,0,0,",
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,NaN,NaN,NaN,NaN,1,0.05,NaN,0,1,1",
    "NSE,55002,RELIANCECE,NSE-RELIANCE-30Jan25-1200-CE,Reliance CE,CE,FNO,NaN,NaN,RELIANCE,2885,2025-01-30,1200,250,0.05,1800,0,1,1",
    "MCX,9999,GOLD,MCX-GOLD,Gold,FUT,FNO,NaN,NaN,GOLD,9999,2025-02-05,NaN,100,1.00,NaN,0,1,1",
]


def _csv(rows: list[str]) -> str:
    """Join *rows* under the canonical header into a CSV text blob."""
    return "\n".join([_HEADER, *rows]) + "\n"


def _store(tmp_path: Path) -> InstrumentStore:
    """Open a fresh, empty real :class:`InstrumentStore` in *tmp_path*."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def test_loads_and_summarises_keyless(tmp_path: Path) -> None:
    """A valid CSV parses, filters MCX, and re-summarises into index/stock counts."""
    store = _store(tmp_path)
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(_ROWS),
        run_id_factory=lambda: "test-load",
    )
    # NIFTY idx + BANKNIFTY idx (real-style) + RELIANCE eq + RELIANCE CE = 4
    # parsed; MCX filtered. The real-style BANKNIFTY row (blank economics,
    # non-numeric token) now parses too (DEC-INDEX-PARSE-FIX-001).
    assert result["parsed"] == 4
    assert result["filtered"] == 1
    assert result["quarantined"] == 0
    # Snapshot re-summary: NIFTY + BANKNIFTY → 2 indices, RELIANCE
    # (F&O-eligible) → 1 stock.
    assert result["index_count"] == 2
    assert result["stock_count"] == 1
    assert result["total"] == 3


def test_default_run_id_factory(tmp_path: Path) -> None:
    """Omitting ``run_id_factory`` uses the built-in default and still loads."""
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv(_ROWS))
    assert result["parsed"] == 4
    assert result["total"] == 3


def test_malformed_header_raises(tmp_path: Path) -> None:
    """A CSV missing a required column aborts with InstrumentFormatError."""
    store = _store(tmp_path)
    bad_header = _HEADER.replace("tick_size,", "")  # drop a required column
    with pytest.raises(InstrumentFormatError, match="missing required columns"):
        load_instrument_master(store, http_get=lambda _url: bad_header + "\n")


def test_empty_csv_yields_zero_counts(tmp_path: Path) -> None:
    """An empty (header-only) CSV loads zero instruments → all counts 0."""
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv([]))
    assert result == {
        "parsed": 0,
        "filtered": 0,
        "quarantined": 0,
        "index_count": 0,
        "stock_count": 0,
        "total": 0,
    }
