"""Integration tests for the keyless instrument-master loader (Dhan).

DEC-VENDOR-PIVOT-001 + DEC-INSTRUMENT-UNIVERSE-FAST-001:

* **Lean universe** — only the universe underlyings (every index + every
  F&O-eligible single-stock SPOT) persist; the FUT/CE/PE legs are parsed for
  eligibility then dropped (recoverable on demand). Each persisted underlying
  is stamped with its **tradable F&O lot + freeze** from the nearest-expiry
  derivative leg (DEC-UNDERLYING-LEG-ECONOMICS-001) — RELIANCE 500, NIFTY 75 —
  superseding the EQUITY/INDEX row's cash lot (1 / 0); the spot ``tick_size``
  is kept (an index spot tick differs from its future's).
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
    (RELIANCE) + its option leg, and a no-leg index (BANKNIFTY)."""
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
            freeze_quantity="1800",
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
            freeze_quantity="10000",
        ),
        # Non-F&O index: no legs.
        _row(
            security_id="99",
            instrument_type="INDEX",
            symbol_name="BANKNIFTY",
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
    assert result["index_count"] == 2  # NIFTY + BANKNIFTY (both allow-listed)
    assert result["stock_count"] == 1  # RELIANCE
    assert result["persisted"] == 3
    assert result["legs_dropped"] == 3  # NIFTY FUT + NIFTY CE + RELIANCE option

    # Each persisted underlying is stamped with its TRADABLE F&O lot + freeze
    # from the nearest-expiry derivative leg (DEC-UNDERLYING-LEG-ECONOMICS-001):
    # RELIANCE → 500 (its OPTSTK leg), NIFTY → 75 (its FUTIDX leg). The EQUITY/
    # INDEX row's own cash lot (1 / 0) + freeze are SUPERSEDED by the leg's; the
    # ``tick_size`` is NOT (an index spot tick differs from its future's). The
    # store is keyed by ``symbol_name`` (the engine symbol); the numeric Dhan id
    # lives in ``security_id`` (ADR-0003 dropped the exchange-prefixed key).
    rel = store.get("RELIANCE")
    assert rel is not None
    assert rel.security_id == 2885
    assert rel.lot_size == 500  # F&O lot from the OPTSTK leg (was cash 1)
    assert str(rel.tick_size) == "0.0500"  # spot tick PRESERVED (not the leg's)
    assert rel.sm_freeze_qty == 10000  # freeze from the leg (was the EQ row's 5000)

    nifty = store.get("NIFTY")
    assert nifty is not None
    assert nifty.lot_size == 75  # F&O lot from the FUTIDX leg (was index 0)
    assert nifty.sm_freeze_qty == 1800

    # BANKNIFTY has NO F&O leg in this fixture → keeps its cash index lot (0).
    n100 = store.get("BANKNIFTY")
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
    rel = store.get("RELIANCE")
    assert (
        rel is not None and rel.lot_size == 500
    )  # enriched F&O lot, deterministic across re-ingest
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

    def record_run(self, _run: Any) -> None:
        """No-op: this probe tests lock serialisation, not run recording."""

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
