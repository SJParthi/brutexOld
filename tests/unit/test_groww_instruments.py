"""Tests for `brutex.adapters.groww.instruments` — instrument-master fetch (Surface B).

A canned CSV (index · future · option · equity · MCX · bad-tick row) + a fake
store prove the fetcher parses in-scope rows, filters out-of-scope exchanges,
quarantines a bad row (keeping the rest), tags fetch-scope, and aborts on
header schema drift — all with no network and no DuckDB (the store is a fake).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.instruments import InstrumentMasterFetcher
from brutex.models import Instrument, InstrumentFormatError, InstrumentType

_HEADER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,"
    "series,isin,underlying_symbol,underlying_exchange_token,expiry_date,strike_price,"
    "lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed"
)
_ROWS = [
    "NSE,26000,NIFTY,NSE-NIFTY,Nifty 50,IDX,CASH,NaN,NaN,NaN,NaN,NaN,NaN,1,0.05,NaN,0,1,1",
    "NSE,53001,NIFTYFUT,NSE-NIFTY-30Jan25-FUT,Nifty Fut,FUT,FNO,NaN,NaN,NIFTY,26000,2025-01-30,NaN,75,0.05,1800,0,1,1",
    "NSE,55001,NIFTYCE,NSE-NIFTY-30Jan25-24650-CE,Nifty CE,CE,FNO,NaN,NaN,NIFTY,26000,2025-01-30,24650,75,0.05,1800,0,1,1",
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance,EQ,CASH,EQ,INE002A01018,NaN,NaN,NaN,NaN,1,0.05,NaN,0,1,1",
    "BSE,1,SENSEX,BSE-SENSEX,Sensex,IDX,CASH,NaN,NaN,NaN,NaN,NaN,NaN,1,0.05,NaN,0,1,1",
    "MCX,9999,GOLD,MCX-GOLD,Gold,FUT,FNO,NaN,NaN,GOLD,9999,2025-02-05,NaN,100,1.00,NaN,0,1,1",
    "NSE,3001,BADEQ,NSE-BADEQ,Bad,EQ,CASH,EQ,INE000000000,NaN,NaN,NaN,NaN,1,0.07,NaN,0,1,1",
]


def _csv(rows: list[str] = _ROWS) -> str:
    return "\n".join([_HEADER, *rows]) + "\n"


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def upsert_many(
        self,
        instruments: Any,
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        in_scope_of: Any = None,
    ) -> dict[str, int]:
        items = list(instruments)
        self.calls.append(
            {
                "instruments": items,
                "snapshot_date": snapshot_date,
                "run_id": run_id,
                "csv_source_sha256": csv_source_sha256,
                "in_scope_of": in_scope_of,
            }
        )
        return {"rows_written": len(items)}


def _fetcher(csv_text: str) -> tuple[InstrumentMasterFetcher, _FakeStore]:
    store = _FakeStore()
    fetcher = InstrumentMasterFetcher(
        http_get=lambda _url: csv_text,
        store=store,
        run_id_factory=lambda: "run-xyz",
    )
    return fetcher, store


class TestRefresh:
    def test_parses_filters_and_quarantines(self) -> None:
        fetcher, store = _fetcher(_csv())
        result = fetcher.refresh()
        # NIFTY idx + fut + CE + RELIANCE + SENSEX = 5 parsed
        assert result.parsed == 5
        assert result.filtered == 1  # MCX
        assert result.quarantined == 1  # tick_size 0.07 invalid
        assert len(result.csv_sha256) == 64
        assert len(store.calls) == 1
        assert len(store.calls[0]["instruments"]) == 5
        assert store.calls[0]["run_id"] == "run-xyz"

    def test_in_scope_callable_uses_fetch_scope(self) -> None:
        fetcher, store = _fetcher(_csv())
        fetcher.refresh()
        in_scope_of = store.calls[0]["in_scope_of"]
        nifty = next(
            i
            for i in store.calls[0]["instruments"]
            if i.instrument_type == InstrumentType.IDX and i.vendor_symbol == "NSE-NIFTY"
        )
        assert in_scope_of(nifty) is True  # an NSE index is in the broad fetch tier

    def test_parsed_instruments_are_typed(self) -> None:
        fetcher, store = _fetcher(_csv())
        fetcher.refresh()
        instruments = store.calls[0]["instruments"]
        assert all(isinstance(i, Instrument) for i in instruments)
        ce = next(i for i in instruments if i.vendor_symbol == "NSE-NIFTY-30Jan25-24650-CE")
        assert ce.strike_price is not None and ce.expiry_date == date(2025, 1, 30)

    def test_snapshot_date_override(self) -> None:
        fetcher, store = _fetcher(_csv())
        fetcher.refresh(snapshot_date=date(2021, 1, 1))
        assert store.calls[0]["snapshot_date"] == date(2021, 1, 1)

    def test_default_snapshot_date_uses_clock(self) -> None:
        store = _FakeStore()
        fetcher = InstrumentMasterFetcher(
            http_get=lambda _u: _csv(),
            store=store,
            run_id_factory=lambda: "r",
            clock=lambda: mock.Mock(date=lambda: date(2026, 5, 28)),
        )
        fetcher.refresh()
        assert store.calls[0]["snapshot_date"] == date(2026, 5, 28)

    def test_empty_csv_upserts_nothing(self) -> None:
        fetcher, store = _fetcher(_csv(rows=[]))
        result = fetcher.refresh()
        assert (result.parsed, result.filtered, result.quarantined) == (0, 0, 0)
        assert store.calls[0]["instruments"] == []


class TestSchemaDrift:
    def test_missing_required_column_aborts_without_upsert(self) -> None:
        bad_header = _HEADER.replace("tick_size,", "")  # drop a required column
        fetcher, store = _fetcher(bad_header + "\n")
        with pytest.raises(InstrumentFormatError, match="missing required columns"):
            fetcher.refresh()
        assert store.calls == []  # store keeps its last good snapshot
