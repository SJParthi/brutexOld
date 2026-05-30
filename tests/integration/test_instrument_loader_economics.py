"""Integration tests for the keyless instrument-master loader fixes.

DEC-UNDERLYING-ECONOMICS-001:

* **FIX 1** — persisted IDX/EQ underlyings are back-filled with the REAL
  lot/tick/freeze harvested from their derivative legs.
* **FIX 3** — concurrent ``load_instrument_master`` calls are serialised by a
  module-level lock so a snapshot wipe can never interleave another load's
  insert (the "25 indices · 0 stocks" corruption).

These run against a real :class:`~brutex.data.instrument_store.InstrumentStore`
(coverage-omit) with an injected ``http_get`` returning canned CSV, so the
loader's new lock branch + the adapter back-fill are exercised end-to-end.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore

_HEADER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,"
    "segment,series,isin,underlying_symbol,underlying_exchange_token,expiry_date,"
    "strike_price,lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,"
    "sell_allowed,internal_trading_symbol"
)


def _row(
    *,
    trading_symbol: str,
    instrument_type: str,
    segment: str = "CASH",
    series: str = "",
    isin: str = "",
    underlying_symbol: str = "",
    expiry_date: str = "",
    strike_price: str = "",
    lot_size: str = "1",
    tick_size: str = "0.05",
    freeze_quantity: str = "",
    exchange: str = "NSE",
) -> str:
    """Build one CSV data row matching the canonical 20-column header."""
    groww = f"{exchange}-{trading_symbol}"
    return ",".join(
        [
            exchange,
            "1",
            trading_symbol,
            groww,
            trading_symbol,
            instrument_type,
            segment,
            series,
            isin,
            underlying_symbol,
            "",
            expiry_date,
            strike_price,
            lot_size,
            tick_size,
            freeze_quantity,
            "false",
            "true",
            "true",
            "",
        ]
    )


def _csv(*rows: str) -> str:
    """Join header + data rows into a CSV body with a trailing newline."""
    return "\n".join([_HEADER, *rows]) + "\n"


def _make_http_get(body: str) -> Any:
    """Return an injectable ``url -> CSV text`` that ignores the URL."""

    def _get(_url: str) -> str:
        return body

    return _get


def _build_store(tmp_path: Path) -> InstrumentStore:
    """Open a fresh on-disk InstrumentStore under *tmp_path*."""
    pytest.importorskip("duckdb")  # gate on the data extra
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def _economics_csv() -> str:
    """Real-master-shaped CSV: blank-economics IDX/EQ underlyings + legs that
    carry the real lot/tick/freeze (RELIANCE 500/5000, NIFTY 75/1800), plus a
    non-F&O index (NIFTY100) with no legs."""
    return _csv(
        # F&O index: blank economics on the IDX row, real values on the legs.
        _row(
            trading_symbol="NIFTY",
            instrument_type="IDX",
            segment="CASH",
            lot_size="",
            tick_size="",
            series="",
            isin="",
            freeze_quantity="",
        ),
        _row(
            trading_symbol="NIFTY25JANFUT",
            instrument_type="FUT",
            segment="FNO",
            underlying_symbol="NIFTY",
            expiry_date="2025-01-30",
            lot_size="75",
            tick_size="0.05",
            freeze_quantity="1800",
        ),
        _row(
            trading_symbol="NIFTY25JAN24000CE",
            instrument_type="CE",
            segment="FNO",
            underlying_symbol="NIFTY",
            expiry_date="2025-01-30",
            strike_price="24000",
            lot_size="75",
            tick_size="0.05",
            freeze_quantity="1800",
        ),
        # F&O stock: EQ row ships lot_size=1 / blank freeze; legs carry 500/5000.
        _row(
            trading_symbol="RELIANCE",
            instrument_type="EQ",
            lot_size="1",
            tick_size="0.05",
            freeze_quantity="",
        ),
        _row(
            trading_symbol="RELIANCE25JANFUT",
            instrument_type="FUT",
            segment="FNO",
            underlying_symbol="RELIANCE",
            expiry_date="2025-01-30",
            lot_size="500",
            tick_size="0.05",
            freeze_quantity="5000",
        ),
        _row(
            trading_symbol="RELIANCE25JAN3000CE",
            instrument_type="CE",
            segment="FNO",
            underlying_symbol="RELIANCE",
            expiry_date="2025-01-30",
            strike_price="3000",
            lot_size="500",
            tick_size="0.05",
            freeze_quantity="5000",
        ),
        # Non-F&O index: no legs → keeps its own master values.
        _row(
            trading_symbol="NIFTY100",
            instrument_type="IDX",
            segment="CASH",
            lot_size="",
            tick_size="",
            series="",
            isin="",
            freeze_quantity="",
        ),
    )


def test_backfills_real_economics(tmp_path: Path) -> None:
    """Persisted IDX/EQ underlyings get the real lot/tick/freeze from their legs."""
    store = _build_store(tmp_path)
    result = load_instrument_master(
        store, http_get=_make_http_get(_economics_csv()), run_id_factory=lambda: "t"
    )
    assert result["index_count"] == 2  # NIFTY + NIFTY100
    assert result["stock_count"] == 1  # RELIANCE

    rel = store.get("NSE-RELIANCE")
    assert rel is not None
    assert rel.lot_size == 500
    assert str(rel.tick_size) == "0.0500"
    assert rel.freeze_quantity == 5000

    nifty = store.get("NSE-NIFTY")
    assert nifty is not None
    assert nifty.lot_size == 75
    assert nifty.freeze_quantity == 1800

    # A non-F&O index has no legs → keeps the master row's own (blank) values.
    n100 = store.get("NSE-NIFTY100")
    assert n100 is not None
    assert n100.lot_size == 0
    assert n100.freeze_quantity is None

    # No FUT/CE/PE leg ever lands (lean ingest fingerprint stays clean).
    assert store.has_derivative_legs() is False
    store.close()


def test_two_sequential_refreshes_deterministic(tmp_path: Path) -> None:
    """Re-ingest yields the SAME full counts (no '25 indices · 0 stocks' loss)."""
    store = _build_store(tmp_path)
    http = _make_http_get(_economics_csv())
    first = load_instrument_master(store, http_get=http, run_id_factory=lambda: "r1")
    second = load_instrument_master(store, http_get=http, run_id_factory=lambda: "r2")
    assert (first["index_count"], first["stock_count"]) == (2, 1)
    assert (second["index_count"], second["stock_count"]) == (2, 1)
    assert store.has_derivative_legs() is False
    # Economics survive the wipe+reinsert on the second refresh.
    rel = store.get("NSE-RELIANCE")
    assert rel is not None and rel.lot_size == 500
    store.close()


class _InterleaveProbeStore:
    """A fake store that records whether two loads ever overlap in the lock.

    ``load_instrument_master`` holds ``_LOAD_LOCK`` for the whole refresh +
    read-back, so a second concurrent call must wait. This fake makes the
    in-lock region observably long (a brief sleep) and flags any overlap, so the
    test proves the server-side lock serialises concurrent loads regardless of
    the backing store's own thread model (DEC-UNDERLYING-ECONOMICS-001 FIX 3).
    """

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._probe_lock = threading.Lock()
        self.upserts = 0

    def upsert_many(self, instruments: Any, **_kwargs: Any) -> Any:
        import time

        with self._probe_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.upserts += 1
        time.sleep(0.02)  # widen the critical section so any overlap is caught
        with self._probe_lock:
            self.active -= 1

        class _Up:
            rows_written = len(instruments)
            rows_replaced = 0

        return _Up()

    def fno_eligible_underlyings(self) -> frozenset[tuple[Any, str]]:
        return frozenset()

    def list_index_equity_instruments(self) -> list[Any]:
        return []


def test_serialised_under_concurrency() -> None:
    """The module lock serialises concurrent loads — they never overlap."""
    store = _InterleaveProbeStore()
    http = _make_http_get(
        _csv(
            _row(
                trading_symbol="NIFTY",
                instrument_type="IDX",
                segment="CASH",
                lot_size="",
                tick_size="",
                series="",
                isin="",
                freeze_quantity="",
            )
        )
    )
    errors: list[BaseException] = []

    def _worker(tag: str) -> None:
        try:
            load_instrument_master(store, http_get=http, run_id_factory=lambda: tag)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(f"c{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert store.upserts == 2  # both loads ran
    assert store.max_active == 1  # never overlapped — the lock serialised them
