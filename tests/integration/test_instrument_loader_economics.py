"""Integration tests for the keyless instrument-master loader (Groww).

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5) +
DEC-INSTRUMENT-UNIVERSE-FAST-001:

* **Lean universe** — only the universe underlyings (every admitted index + every
  F&O-eligible single-stock SPOT) persist; the FUT/CE/PE legs are parsed for
  eligibility then dropped (recoverable on demand). Unlike the retired Dhan
  loader, the Groww :class:`~brutex.adapters.groww.instruments.InstrumentIndex`
  carries NO nearest-expiry leg-economics enrichment (the Dhan-only
  DEC-UNDERLYING-LEG-ECONOMICS-001 lot/freeze fold has no Groww twin): each
  persisted underlying keeps ITS OWN row's lot / tick / freeze.
* **Concurrency (FIX 3)** — concurrent ``load_instrument_master`` calls are
  serialised by a module-level lock so a snapshot wipe can never interleave
  another load's insert (the "25 indices · 0 stocks" corruption).

These run against a real :class:`~brutex.data.instrument_store.InstrumentStore`
(coverage-omit) with an injected ``http_get`` returning a canned **Groww
instrument-master** CSV.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
from brutex.dashboard.instrument_loader import load_instrument_master
from brutex.data.instrument_store import InstrumentStore

#: The canonical 21-column Groww instrument-master header.
_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _row(**over: str) -> str:
    """Build one 21-field Groww CSV row; every column defaults blank."""
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


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
    """Groww-shaped CSV: a F&O index (NIFTY) + its FUT/CE legs, a F&O stock
    (RELIANCE) made eligible by an option leg, and a no-leg index (BANKNIFTY).

    Unlike the Dhan fixture, the persisted underlyings keep their OWN row
    economics (no leg-fold), so NIFTY/BANKNIFTY carry the blank index lot/freeze
    (0) and RELIANCE carries its own EQ lot/tick/freeze.
    """
    return _csv(
        _row(
            exchange="NSE",
            exchange_token="NIFTY",
            trading_symbol="NIFTY",
            groww_symbol="NSE-NIFTY",
            name="NIFTY 50",
            instrument_type="IDX",
            segment="CASH",
        ),
        _row(
            exchange="NSE",
            exchange_token="40001",
            trading_symbol="NIFTY-FUT",
            groww_symbol="NSE-NIFTY-FUT",
            instrument_type="FUT",
            segment="FNO",
            underlying_symbol="NIFTY",
            underlying_exchange_token="13",
            expiry_date="2025-01-30",
            lot_size="75",
            tick_size="0.05",
            freeze_quantity="1800",
        ),
        _row(
            exchange="NSE",
            exchange_token="40002",
            trading_symbol="NIFTY-CE",
            groww_symbol="NSE-NIFTY-CE",
            instrument_type="CE",
            segment="FNO",
            underlying_symbol="NIFTY",
            underlying_exchange_token="13",
            strike_price="24000",
            expiry_date="2025-01-30",
            lot_size="75",
            tick_size="0.05",
        ),
        # F&O stock: the EQ row carries its OWN lot/tick/freeze (lot 1, kept as-is).
        _row(
            exchange="NSE",
            exchange_token="2885",
            trading_symbol="RELIANCE",
            groww_symbol="NSE-RELIANCE",
            name="Reliance Industries",
            instrument_type="EQ",
            segment="CASH",
            series="EQ",
            isin="INE002A01018",
            lot_size="1",
            tick_size="0.05",
            freeze_quantity="5000",
        ),
        _row(
            exchange="NSE",
            exchange_token="40003",
            trading_symbol="RELIANCE-CE",
            groww_symbol="NSE-RELIANCE-CE",
            instrument_type="CE",
            segment="FNO",
            underlying_symbol="RELIANCE",
            underlying_exchange_token="2885",
            strike_price="3000",
            expiry_date="2025-01-30",
            lot_size="500",
            tick_size="0.05",
            freeze_quantity="10000",
        ),
        # Non-F&O index: no legs.
        _row(
            exchange="NSE",
            exchange_token="BANKNIFTY",
            trading_symbol="BANKNIFTY",
            groww_symbol="NSE-BANKNIFTY",
            name="NIFTY Bank",
            instrument_type="IDX",
            segment="CASH",
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

    # The Groww loader carries NO nearest-expiry leg-economics fold (the Dhan-only
    # DEC-UNDERLYING-LEG-ECONOMICS-001 has no Groww twin — Golden Rule #1): each
    # persisted underlying keeps ITS OWN row's lot / tick / freeze. The store is
    # keyed by ``symbol_name`` (the engine symbol = Groww ``trading_symbol``); the
    # numeric id lives in ``security_id`` (the Groww ``exchange_token``).
    rel = store.get("RELIANCE")
    assert rel is not None
    assert rel.security_id == 2885
    assert rel.lot_size == 1  # the EQ row's OWN cash lot (no leg fold)
    assert str(rel.tick_size) == "0.0500"  # the EQ row's own spot tick
    assert rel.sm_freeze_qty == 5000  # the EQ row's own freeze

    nifty = store.get("NIFTY")
    assert nifty is not None
    assert nifty.lot_size == 0  # the index row's own (blank → 0) lot, no FUT fold
    assert nifty.sm_freeze_qty is None  # no freeze on the blank index row

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
    assert rel is not None and rel.lot_size == 1  # its own EQ lot, deterministic across re-ingest
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
                exchange="NSE",
                exchange_token="NIFTY",
                trading_symbol="NIFTY",
                groww_symbol="NSE-NIFTY",
                name="NIFTY 50",
                instrument_type="IDX",
                segment="CASH",
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
