"""Oracle: dashboard reads run on a child cursor + read lock (DEC-STORE-READ-WRITE-SPLIT-001).

PR-A (#437) moved the blocking read off the event loop (onto a worker thread) but
the read still WAITED behind the writer's single ``RLock`` — it just waited on a
worker. This splits the dashboard ``store.connection`` read onto a dedicated child
READ cursor under a SEPARATE read lock, so an uncached ``/db`` scan no longer waits
for the writer at all, and DuckDB MVCC hands it a consistent COMMITTED snapshot
even while the writer holds an open transaction.

Two oracles: (1) a read completes far under a held WRITE-lock hold (it did not
wait); (2) a read during the writer's open, uncommitted transaction sees the
committed snapshot, never the writer's dirty rows.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading
import time
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

#: The writer holds the WRITE lock this long; the read budget is far below it, so a
#: passing read PROVES it did not wait (the old single-lock path would wait ~the
#: full hold and fail).
_WRITER_HOLD_S: float = 2.0
_READ_BUDGET_S: float = 0.3


def _idx(symbol: str, sid: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=sid,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    """A real store on a tmp lake holding two index rows (committed)."""
    s = InstrumentStore.open(tmp_path / "lake" / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY", 13), _idx("BANKNIFTY", 25)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_read_does_not_wait_behind_the_writer_lock(store: InstrumentStore) -> None:
    """An uncached store.connection read completes while a writer holds the WRITE lock."""
    holding = threading.Event()
    release = threading.Event()

    def _hold_write_lock() -> None:
        with store._lock:  # the WRITE RLock a long synchronized write would hold
            holding.set()
            release.wait(timeout=_WRITER_HOLD_S + 3.0)

    writer = threading.Thread(target=_hold_write_lock)
    writer.start()
    try:
        assert holding.wait(timeout=2.0)
        start = time.perf_counter()
        with store.connection as conn:
            rows = conn.execute("SELECT count(*) FROM instruments").fetchall()
        elapsed = time.perf_counter() - start
        assert rows[0][0] == 2  # a consistent committed snapshot
        assert elapsed < _READ_BUDGET_S, f"read waited {elapsed:.3f}s behind the writer"
    finally:
        release.set()
        writer.join(timeout=5.0)


def test_read_sees_committed_snapshot_during_open_write_txn(store: InstrumentStore) -> None:
    """A reader sees the COMMITTED snapshot, never the writer's uncommitted rows (MVCC)."""
    store._conn.execute("BEGIN TRANSACTION")
    store._conn.execute("DELETE FROM instruments")  # uncommitted on the writer
    try:
        with store.connection as conn:
            mid = conn.execute("SELECT count(*) FROM instruments").fetchone()[0]
        assert mid == 2  # MVCC: the uncommitted DELETE is INVISIBLE to the reader
    finally:
        store._conn.execute("ROLLBACK")
    # After rollback the reader still sees the original committed two rows.
    with store.connection as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 2
