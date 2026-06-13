"""Tests for ``brutex.dashboard.instrument_loader`` — keyless Groww load.

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): the keyless
instrument-master load was repointed from the Dhan 31-column detailed scrip
master to the Groww 21-column instrument master. These tests inject a fake
``http_get`` returning a small valid **Groww instrument master** CSV against a
REAL temp :class:`~brutex.data.instrument_store.InstrumentStore` and assert the
loader parses/filters correctly, persists the LEAN universe (IDX + F&O-eligible
EQ; legs dropped), re-summarises the stored snapshot into ``index_count`` /
``stock_count``, aborts on a malformed header, handles an empty CSV, and reports
NTM as a deliberate Groww no-op. NO Groww token / creds anywhere — the master is
a public CSV (DEC-VENDOR-PIVOT-001).

CSV patterns follow ``tests/unit/test_groww_instruments.py``; the real store
usage follows ``tests/integration/test_universe_explorer_store.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore
from brutex.models import InstrumentFormatError

_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    """Build one 21-field Groww CSV row; every column defaults blank."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


# Two NSE indices (IDX, lot/tick blank → 0), one F&O-eligible stock (WIPRO) made
# eligible by a WIPRO option leg, plus a WIPRO CE leg, and one out-of-scope MCX
# COMMODITY row that the Groww parser skips BEFORE the index. The lean universe
# persists the 2 indices + WIPRO (3); the option leg is dropped; MCX is filtered.
_NIFTY = _row(
    exchange="NSE",
    exchange_token="NIFTY",
    trading_symbol="NIFTY",
    groww_symbol="NSE-NIFTY",
    name="NIFTY 50",
    instrument_type="IDX",
    segment="CASH",
)
_BANKNIFTY = _row(
    exchange="NSE",
    exchange_token="BANKNIFTY",
    trading_symbol="BANKNIFTY",
    groww_symbol="NSE-BANKNIFTY",
    name="NIFTY Bank",
    instrument_type="IDX",
    segment="CASH",
)
_WIPRO = _row(
    exchange="NSE",
    exchange_token="3787",
    trading_symbol="WIPRO",
    groww_symbol="NSE-WIPRO",
    name="Wipro",
    instrument_type="EQ",
    segment="CASH",
    series="EQ",
    isin="INE075A01022",
    lot_size="1",
    tick_size="0.05",
)
_WIPRO_CE = _row(
    exchange="NSE",
    exchange_token="50467",
    trading_symbol="WIPRO2661621800CE",
    groww_symbol="NSE-WIPRO-16Jun26-300-CE",
    instrument_type="CE",
    segment="FNO",
    underlying_symbol="WIPRO",
    underlying_exchange_token="3787",
    expiry_date="2026-06-16",
    strike_price="300",
    lot_size="1500",
    tick_size="0.05",
)
_COMMODITY = _row(
    exchange="MCX",
    exchange_token="1",
    trading_symbol="GOLD",
    groww_symbol="MCX-GOLD",
    instrument_type="FUT",
    segment="COMMODITY",
    underlying_symbol="GOLD",
    expiry_date="2026-08-25",
    lot_size="100",
    tick_size="1",
)

_ROWS = [_NIFTY, _BANKNIFTY, _WIPRO, _WIPRO_CE, _COMMODITY]


def _csv(rows: list[str]) -> str:
    """Join *rows* under the canonical Groww header into a CSV text blob."""
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
    # NIFTY idx + BANKNIFTY idx + WIPRO eq + WIPRO CE = 4 in-scope rows parsed;
    # the MCX COMMODITY row is out-of-scope → skipped by the Groww parser.
    assert result["parsed"] == 4
    assert result["filtered"] == 0  # Groww parser skips out-of-scope before the index
    assert result["quarantined"] == 0  # Groww fails closed — no quarantine bucket
    # Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001): NIFTY + BANKNIFTY (IDX)
    # + WIPRO (F&O-eligible EQ) persist (3); the WIPRO CE option leg is dropped.
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 1
    # Summary over the persisted universe underlyings: 2 indices, 1 stock.
    assert result["index_count"] == 2
    assert result["stock_count"] == 1
    assert result["total"] == 3
    # NTM auto-admission has no Groww twin → a deliberate Groww no-op note.
    assert result["ntm"]["admitted"] is False
    assert "no Groww equivalent" in result["ntm"]["reason"]


def test_default_run_id_factory(tmp_path: Path) -> None:
    """Omitting ``run_id_factory`` uses the built-in default and still loads."""
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv(_ROWS))
    assert result["parsed"] == 4
    assert result["total"] == 3


def test_malformed_header_raises(tmp_path: Path) -> None:
    """A CSV with a drifted Groww header aborts with InstrumentFormatError."""
    store = _store(tmp_path)
    bad_header = _HEADER.replace("tick_size,", "")  # drop a required column
    with pytest.raises(InstrumentFormatError, match="header drift"):
        load_instrument_master(store, http_get=lambda _url: bad_header + "\n")


def test_empty_csv_yields_zero_counts(tmp_path: Path) -> None:
    """An empty (header-only) CSV loads zero instruments → all counts 0."""
    store = _store(tmp_path)
    result = load_instrument_master(store, http_get=lambda _url: _csv([]))
    ntm = result.pop("ntm")
    assert ntm["admitted"] is False
    assert "no Groww equivalent" in ntm["reason"]
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


def test_non_fno_equity_dropped_from_lean_universe(tmp_path: Path) -> None:
    """A non-F&O equity (no derivative names it) is NOT persisted (lean ingest).

    Mirrors DEC-INSTRUMENT-UNIVERSE-FAST-001: only F&O-eligible equities join the
    indices; a cash equity with no option/future leg is parsed (so it is counted
    in ``parsed``) but dropped from the persisted universe (``legs_dropped``).
    """
    store = _store(tmp_path)
    non_fno = _row(
        exchange="NSE",
        exchange_token="9999",
        trading_symbol="SMALLCO",
        groww_symbol="NSE-SMALLCO",
        name="SmallCo Ltd",
        instrument_type="EQ",
        segment="CASH",
        series="EQ",
        isin="INE999A01010",
        lot_size="1",
        tick_size="0.05",
    )
    result = load_instrument_master(store, http_get=lambda _url: _csv([*_ROWS, non_fno]))
    # 5 in-scope rows parsed (NIFTY, BANKNIFTY, WIPRO, WIPRO CE, SMALLCO).
    assert result["parsed"] == 5
    # WIPRO CE leg + the non-F&O SMALLCO equity are both dropped from the lean set.
    assert result["legs_dropped"] == 2
    assert result["persisted"] == 3
    # SMALLCO never reaches the explorer — still 2 indices + 1 stock (WIPRO).
    assert result["index_count"] == 2
    assert result["stock_count"] == 1
