"""Tests for `brutex.adapters.groww.instruments` — instrument-master fetch (Surface B).

A canned CSV (index · future · option · equity · MCX · bad-tick row) + a fake
store prove the fetcher parses in-scope rows, filters out-of-scope exchanges,
quarantines a bad row (keeping the rest), tags fetch-scope, and aborts on
header schema drift — all with no network and no DuckDB (the store is a fake).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex.adapters.groww.instruments import (
    InstrumentMasterFetcher,
    _req_int_or_zero,
)
from brutex.models import (
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    TickSizeFormatError,
)

_REAL_IDX_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "groww_idx_rows_real.csv"

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


class TestTestInstrumentFilter:
    """Groww internal test instruments (NSETEST / negative strike) are filtered."""

    def test_nsetest_rows_are_filtered(self) -> None:
        junk = (
            "NSE,36691,031NSETEST36DECFUT,NSE-031NSETEST-27Nov36-FUT,,FUT,FNO,"
            "NaN,NaN,031NSETEST,14753,2036-11-27,-0.01,50,0.05,100001,0,1,1"
        )
        fetcher, store = _fetcher(_csv([*_ROWS, junk]))
        result = fetcher.refresh()
        # baseline: 5 parsed / 1 filtered (MCX) / 1 quarantined; the junk row adds 1 filtered
        assert result.parsed == 5
        assert result.filtered == 2
        # no NSETEST instrument reached the store
        assert all(
            "NSETEST" not in (inst.trading_symbol or "").upper()
            for inst in store.calls[0]["instruments"]
        )

    def test_negative_strike_row_is_filtered(self) -> None:
        # No NSETEST marker — isolates the negative-strike branch.
        junk = (
            "NSE,77777,WEIRDCE,NSE-WEIRD-30Jan25-100-CE,Weird,CE,FNO,"
            "NaN,NaN,WEIRD,26000,2025-01-30,-5,75,0.05,1800,0,1,1"
        )
        fetcher, _store = _fetcher(_csv([*_ROWS, junk]))
        result = fetcher.refresh()
        assert result.parsed == 5
        assert result.filtered == 2  # MCX + negative-strike junk


class TestSchemaDrift:
    def test_missing_required_column_aborts_without_upsert(self) -> None:
        bad_header = _HEADER.replace("tick_size,", "")  # drop a required column
        fetcher, store = _fetcher(bad_header + "\n")
        with pytest.raises(InstrumentFormatError, match="missing required columns"):
            fetcher.refresh()
        assert store.calls == []  # store keeps its last good snapshot


class TestRealIndexRows:
    """Regression for DEC-INDEX-PARSE-FIX-001.

    The operator's REAL Groww ``instrument.csv`` carries 30 ``IDX`` rows with
    BLANK ``lot_size`` / ``tick_size`` and a non-numeric ``exchange_token``
    (a SYMBOL string for NSE indices). Before the fix all 30 quarantined and
    the dashboard showed "0 INDEX". They must now all parse.
    """

    def test_real_index_rows_all_parse(self) -> None:
        csv_text = _REAL_IDX_FIXTURE.read_text(encoding="utf-8")
        fetcher, store = _fetcher(csv_text)
        result = fetcher.refresh()
        assert result.parsed == 30
        assert result.quarantined == 0
        instruments = store.calls[0]["instruments"]
        vendor_symbols = {i.vendor_symbol for i in instruments}
        for expected in (
            "NSE-NIFTY",
            "NSE-BANKNIFTY",
            "BSE-SENSEX",
            "NSE-INDIAVIX",
            "BSE-BANKEX",
            "NSE-FINNIFTY",
        ):
            assert expected in vendor_symbols
        # A non-numeric-token index (NIFTYMETAL) → exchange_token 0; SENSEX
        # carries the numeric token 1; blank economics → lot 0 / tick 0.
        metal = next(i for i in instruments if i.vendor_symbol == "NSE-NIFTYMETAL")
        assert metal.exchange_token == 0
        assert metal.lot_size == 0
        assert metal.tick_size == Decimal("0")
        sensex = next(i for i in instruments if i.vendor_symbol == "BSE-SENSEX")
        assert sensex.exchange_token == 1


class TestReqIntOrZero:
    """The new tolerant ``exchange_token`` helper (index symbol-token → 0)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("26000", 26000),
            ("NIFTYMETAL", 0),
            ("1", 1),
            ("75.0", 75),
            ("", 0),
        ],
    )
    def test_parse(self, raw: str, expected: int) -> None:
        assert _req_int_or_zero(raw) == expected


class TestIndexToleranceIsIndexOnly:
    """Index-tolerance must NOT loosen strictness for EQ / option rows.

    Covers BOTH branches of each new ``if is_index`` guard in
    ``_row_to_instrument`` so the module stays 100% line + branch.
    """

    def test_blank_lot_size_on_eq_still_quarantines(self) -> None:
        # EQ row (non-index) with a BLANK lot_size → _req_int("") → quarantine.
        bad = (
            "NSE,3001,BADLOT,NSE-BADLOT,Bad,EQ,CASH,EQ,INE000000000,NaN,NaN,NaN,NaN,,0.05,NaN,0,1,1"
        )
        fetcher, _store = _fetcher(_csv([bad]))
        result = fetcher.refresh()
        assert result.parsed == 0
        assert result.quarantined == 1

    def test_blank_tick_size_on_eq_still_quarantines(self) -> None:
        # EQ row (non-index) with a BLANK tick_size → normalise_tick_size("")
        # → TickSizeFormatError → quarantine.
        bad = (
            "NSE,3002,BADTICK,NSE-BADTICK,Bad,EQ,CASH,EQ,INE000000001,NaN,NaN,NaN,NaN,1,,NaN,0,1,1"
        )
        fetcher, _store = _fetcher(_csv([bad]))
        result = fetcher.refresh()
        assert result.parsed == 0
        assert result.quarantined == 1

    def test_blank_economics_on_eq_raises_tick_error(self) -> None:
        # Sanity: a blank tick on EQ raises TickSizeFormatError (the error
        # type quarantine relies on) — keeps the strict path explicit.
        from brutex.adapters.groww.instruments import _row_to_instrument

        raw = "NSE,3,X,NSE-X,X,EQ,CASH,EQ,I,NaN,NaN,NaN,NaN,1,,NaN,0,1,1"
        row = dict(zip(_HEADER.split(","), raw.split(","), strict=True))
        with pytest.raises(TickSizeFormatError):
            _row_to_instrument(row)
