"""Integration tests for the keyless instrument-master loader (Dhan).

DEC-VENDOR-PIVOT-001 + DEC-INSTRUMENT-UNIVERSE-FAST-001:

* **Lean universe** — only the universe underlyings (every index + every
  F&O-eligible single-stock SPOT) persist; the FUT/CE/PE legs are parsed for
  eligibility then dropped (recoverable on demand). The persisted underlying
  carries **its own** scrip-master economics (lot/tick/freeze); the F&O lot
  lives on the option contract, recovered on demand — there is no leg
  back-fill (that was a Groww-adapter quirk, gone with the pivot).
* **Concurrency (FIX 3)** — concurrent ``load_instrument_master`` calls are
  serialised by a module-level lock so a snapshot wipe can never interleave
  another load's insert (the "25 indices · 0 stocks" corruption).

These run against a real :class:`~brutex.data.instrument_store.InstrumentStore`
(coverage-omit) with an injected ``http_get`` returning a canned **Dhan
detailed scrip master** CSV.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore

# Dhan detailed scrip-master header (required columns + the optional ones used).
_HEADER = (
    "EXCH_ID,SECURITY_ID,INSTRUMENT,SYMBOL_NAME,DISPLAY_NAME,LOT_SIZE,TICK_SIZE,"
    "SM_FREEZE_QTY,UNDERLYING_SYMBOL,OPTION_TYPE,STRIKE_PRICE,SM_EXPIRY_DATE,SERIES,ISIN,"
    "UNDERLYING_SECURITY_ID,ASM_GSM_FLAG"
)


def _row(
    *,
    security_id: str,
    instrument_type: str,
    symbol_name: str,
    lot_size: str = "1",
    tick_size: str = "0.05",
    freeze_quantity: str = "",
    underlying_symbol: str = "",
    option_type: str = "",
    strike_price: str = "",
    expiry_date: str = "",
    series: str = "",
    isin: str = "",
    underlying_security_id: str = "",
    exchange: str = "NSE",
) -> str:
    """Build one CSV data row matching the canonical 16-column Dhan header."""
    return ",".join(
        [
            exchange,
            security_id,
            instrument_type,
            symbol_name,
            symbol_name,  # DISPLAY_NAME
            lot_size,
            tick_size,
            freeze_quantity,
            underlying_symbol,
            option_type,
            strike_price,
            expiry_date,
            series,
            isin,
            underlying_security_id,
            "",  # ASM_GSM_FLAG
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
    """Dhan-shaped CSV: a F&O index (NIFTY) + its FUT/CE legs, a F&O stock
    (RELIANCE) + its option leg, and a no-leg index (NIFTY100)."""
    return _csv(
        _row(
            security_id="13",
            instrument_type="INDEX",
            symbol_name="NIFTY",
            lot_size="0",
            tick_size="0",
        ),
        _row(
            security_id="40001",
            instrument_type="FUTIDX",
            symbol_name="NIFTY-FUT",
            underlying_symbol="NIFTY",
            underlying_security_id="13",
            expiry_date="2025-01-30",
            lot_size="75",
        ),
        _row(
            security_id="40002",
            instrument_type="OPTIDX",
            symbol_name="NIFTY-CE",
            underlying_symbol="NIFTY",
            underlying_security_id="13",
            option_type="CE",
            strike_price="24000",
            expiry_date="2025-01-30",
            lot_size="75",
        ),
        # F&O stock: the EQUITY row carries its own lot/tick/freeze (lot 1).
        # Real CSV: SYMBOL_NAME = long company name, UNDERLYING_SYMBOL = ticker
        # (the F&O join key — DEC-DHAN-FNO-JOIN-KEY-001).
        _row(
            security_id="2885",
            instrument_type="EQUITY",
            symbol_name="RELIANCE INDUSTRIES LTD",
            underlying_symbol="RELIANCE",
            lot_size="1",
            tick_size="0.05",
            freeze_quantity="5000",
            series="EQ",
            isin="INE002A01018",
        ),
        _row(
            security_id="40003",
            instrument_type="OPTSTK",
            symbol_name="RELIANCE-CE",
            underlying_symbol="RELIANCE",
            underlying_security_id="2885",
            option_type="CE",
            strike_price="3000",
            expiry_date="2025-01-30",
            lot_size="500",
        ),
        # Non-F&O index: no legs.
        _row(
            security_id="99",
            instrument_type="INDEX",
            symbol_name="NIFTY100",
            lot_size="0",
            tick_size="0",
        ),
    )


def test_persists_underlying_economics(tmp_path: Path) -> None:
    """The lean universe persists each underlying with its OWN row economics."""
    store = _build_store(tmp_path)
    result = load_instrument_master(
        store, http_get=_make_http_get(_economics_csv()), run_id_factory=lambda: "t"
    )
    assert result["index_count"] == 2  # NIFTY + NIFTY100
    assert result["stock_count"] == 1  # RELIANCE
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 3  # NIFTY FUT + NIFTY CE + RELIANCE option

    # The persisted underlyings carry their own scrip-master economics — the
    # F&O lot lives on the (dropped) option contract, recovered on demand.
    # vendor_symbol is the exchange-prefixed global key (the raw SECURITY_ID
    # lives in exchange_token) — DEC-DHAN-VENDOR-SYMBOL-UNIQUE-001.
    rel = store.get("NSE-RELIANCE")
    assert rel is not None
    assert rel.exchange_token == 2885
    assert rel.lot_size == 1
    assert str(rel.tick_size) == "0.0500"
    assert rel.freeze_quantity == 5000

    nifty = store.get("NSE-NIFTY")
    assert nifty is not None
    assert nifty.lot_size == 0

    n100 = store.get("NSE-NIFTY100")
    assert n100 is not None
    assert n100.lot_size == 0

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
    rel = store.get("NSE-RELIANCE")
    assert rel is not None and rel.lot_size == 1
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

    def prune_to_latest_snapshot(self) -> int:
        return 0

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
                security_id="13",
                instrument_type="INDEX",
                symbol_name="NIFTY",
                lot_size="0",
                tick_size="0",
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
