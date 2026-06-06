"""Tests for ``brutex.dashboard.instrument_loader`` — keyless instrument load.

Injects a fake ``http_get`` returning a small valid **Dhan detailed scrip
master** CSV against a REAL temp
:class:`~brutex.data.instrument_store.InstrumentStore` and asserts the loader
parses/filters correctly, re-summarises the stored snapshot into
``index_count`` / ``stock_count``, aborts on a malformed header, and handles
an empty CSV. NO Dhan token / creds anywhere — the master is a public CSV
(DEC-VENDOR-PIVOT-001).

CSV patterns follow ``tests/unit/test_dhan_instruments.py``; the real store
usage follows ``tests/integration/test_universe_explorer_store.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore
from brutex.models import InstrumentFormatError

# Dhan detailed scrip-master header (the columns the parser requires + the
# optional ones these rows use).
_HEADER = (
    "EXCH_ID,SECURITY_ID,INSTRUMENT,SYMBOL_NAME,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,"
    "SM_FREEZE_QTY,UNDERLYING_SYMBOL,OPTION_TYPE,STRIKE_PRICE,SM_EXPIRY_DATE,SERIES,ISIN,"
    "UNDERLYING_SECURITY_ID,ASM_GSM_FLAG"
)
# Two NSE indices (lot/tick 0), one F&O-eligible stock (RELIANCE) + a RELIANCE
# option leg that makes the stock F&O-eligible, plus one out-of-scope MCX row
# that must be filtered (not parsed). The lean universe persists the 2 indices
# + RELIANCE (3); the option leg is dropped; MCX is filtered.
_ROWS = [
    "NSE,13,INDEX,NIFTY,Nifty 50,0,0,,,,,,,,,",
    "NSE,25,INDEX,BANKNIFTY,Nifty Bank,0,0,,,,,,,,,",
    "NSE,2885,EQUITY,RELIANCE INDUSTRIES LTD,Reliance,1,0.05,,RELIANCE,,,,EQ,INE002A01018,,",
    "NSE,3001,OPTSTK,RELIANCE-OPT,Reliance CE,250,0.05,1800,RELIANCE,CE,2600,2025-01-30,,,2885,",
    "MCX,7777,FUTCOM,GOLD,Gold,100,1,,GOLD,,,,,,7777,",
]


def _csv(rows: list[str]) -> str:
    """Join *rows* under the canonical Dhan header into a CSV text blob."""
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
    # NIFTY idx + BANKNIFTY idx + RELIANCE eq + RELIANCE option = 4 in-scope
    # rows parsed; the MCX row is out-of-scope → filtered.
    assert result["parsed"] == 4
    assert result["filtered"] == 1
    assert result["quarantined"] == 0  # Dhan fails closed — no quarantine bucket
    # Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001): NIFTY + BANKNIFTY (INDEX)
    # + RELIANCE (F&O-eligible EQUITY) persist (3); the RELIANCE option leg is
    # dropped.
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 1
    # Summary over the persisted universe underlyings: 2 indices, 1 stock.
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
    bad_header = _HEADER.replace("TICK_SIZE,", "")  # drop a required column
    with pytest.raises(InstrumentFormatError, match="missing required columns"):
        load_instrument_master(store, http_get=lambda _url: bad_header + "\n")


def test_empty_csv_yields_zero_counts(tmp_path: Path) -> None:
    """An empty (header-only) CSV loads zero instruments → all counts 0."""
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv([]))
    # NTM enrichment skips an empty master (no INDEX row) with no network call.
    assert result.pop("ntm") == {
        "admitted": False,
        "error": (
            "no NSE INDEX row matched /TOTAL\\s*M(?:AR)?K(?:E)?T/i among 0 NSE INDEX "
            "rows — loosen the pattern or confirm the index is present in the master"
        ),
    }
    assert result == {
        "parsed": 0,
        "persisted": 0,
        "legs_dropped": 0,
        "filtered": 0,
        "quarantined": 0,
        "index_count": 0,
        "stock_count": 0,
        "total": 0,
    }


def test_ntm_admitted_with_constituents(tmp_path: Path) -> None:
    """NTM is auto-admitted on the keyless load (DEC-NIFTY-TOTAL-MARKET-LOAD-001).

    The master carries an NTM ``INDEX`` row (id 46) + two NSE equities whose ISINs
    appear in the injected constituent CSV. After the load: NTM is a recognised
    index (index_count 3) and its constituents are admitted as REFERENCE_ONLY
    stocks — exactly what the operator expects to SEE in /universe + /db.
    """
    store = _store(tmp_path)
    rows = [
        *_ROWS,
        "NSE,46,INDEX,NIFTY TOTAL MKT,Nifty Total Market,0,0,,,,,,,,,",
        "NSE,5001,EQUITY,SMALLCO ONE LTD,SmallCo One,1,0.05,,,,,,EQ,INE111A01011,,",
        "NSE,5002,EQUITY,SMALLCO TWO LTD,SmallCo Two,1,0.05,,,,,,EQ,INE222A01012,,",
    ]
    constituent_csv = (
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "SmallCo One Ltd.,X,SMALLCO1,EQ,INE111A01011\n"
        "SmallCo Two Ltd.,Y,SMALLCO2,EQ,INE222A01012\n"
        # RELIANCE is already F&O-eligible (in _ROWS); include it so the basket
        # overlaps the existing universe too.
        "Reliance Industries Ltd.,Energy,RELIANCE,EQ,INE002A01018\n"
    )
    result = load_instrument_master(
        store,
        http_get=lambda _url: _csv(rows),
        constituent_opener=lambda _url: constituent_csv,
    )

    assert result["ntm"]["admitted"] is True
    assert result["ntm"]["index_security_id"] == 46
    assert result["ntm"]["index_symbol"] == "NIFTY TOTAL MKT"
    assert result["ntm"]["constituents"] == 3  # all three ISINs resolved
    # NTM shows up as a third index (NIFTY + BANKNIFTY + NIFTY TOTAL MKT) …
    assert result["index_count"] == 3
    # … and the two non-F&O constituents joined RELIANCE under stocks.
    assert result["stock_count"] == 3


def test_ntm_index_only_when_constituent_fetch_fails(tmp_path: Path) -> None:
    """A constituent-fetch failure degrades to INDEX-ONLY — NTM still appears,
    the core load is unbroken, and the reason is surfaced."""
    store = _store(tmp_path)
    rows = [*_ROWS, "NSE,46,INDEX,NIFTY TOTAL MKT,Nifty Total Market,0,0,,,,,,,,,"]

    def _boom(_url: str) -> str:
        raise OSError("nseindia 403 Forbidden")

    result = load_instrument_master(
        store, http_get=lambda _url: _csv(rows), constituent_opener=_boom
    )
    assert result["ntm"]["admitted"] is True
    assert result["ntm"]["constituents"] == 0
    assert "constituent resolution failed" in result["ntm"]["constituent_note"]
    # NTM the index is still admitted (NIFTY + BANKNIFTY + NTM = 3).
    assert result["index_count"] == 3


def test_ntm_index_only_when_constituent_csv_empty(tmp_path: Path) -> None:
    """An empty constituent body (opener returns "") → INDEX-ONLY admission with
    the 'no constituent CSV mapping' note; NTM the index still appears."""
    store = _store(tmp_path)
    rows = [*_ROWS, "NSE,46,INDEX,NIFTY TOTAL MKT,Nifty Total Market,0,0,,,,,,,,,"]
    result = load_instrument_master(
        store, http_get=lambda _url: _csv(rows), constituent_opener=lambda _url: ""
    )
    assert result["ntm"]["admitted"] is True
    assert result["ntm"]["constituents"] == 0
    assert result["ntm"]["constituent_note"] == "no constituent CSV mapping for NIFTY TOTAL MARKET"
    assert result["index_count"] == 3
