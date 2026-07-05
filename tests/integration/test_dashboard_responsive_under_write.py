"""Responsive-under-write regression oracle (DEC-DASHBOARD-FASTREAD-001).

THE regression test for the dashboard SPEED CORE: prove the read pages no longer
freeze while a data pull holds the store lock. Before this PR every read route
was ``async def`` but called a BLOCKING DuckDB scan directly on the event loop,
and the coverage summary re-globbed the whole lake on every 4 s poll — so one
scan (or a read queued behind the active writer's store lock) froze the entire
async server for the duration of the write.

This test reproduces the writer's hold and asserts two reads stay fast:

* ``GET /api/v1/state`` — a pure in-memory snapshot. It must return ~instantly
  even while a background thread holds the store lock for ``_WRITER_HOLD_S``,
  proving the event loop is no longer serialised behind the writer (every
  blocking read is now offloaded via ``run_in_threadpool``, so the loop stays
  free).
* ``GET /api/v1/coverage/spot`` — primed once so the SummaryCache serves it from
  memory (O(1)) within the 2 s TTL floor. A cache-served poll touches neither the
  lake nor the store lock, so it returns 200 well under the writer's hold time.

Whitebox: the test grabs the store's private ``_lock`` (the same RLock the
``_LockedReadConn`` acquires) to simulate the writer — private access is allowed
in tests (file-top ``reportPrivateUsage=none`` pragma; ruff does not enable
``SLF001``), mirroring the existing whitebox store suites.

Skipped when ``fastapi`` / ``httpx2`` / ``duckdb`` are absent (matches the other
dashboard integration suites).
"""

# The writer simulation holds the store's private `_lock` (whitebox).
# pyright: reportPrivateUsage=none
from __future__ import annotations

import threading
import time
from collections.abc import Generator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx2")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

#: The writer holds the store lock this long. The read assertions use a budget a
#: large factor below it, so a passing read PROVES it did not wait for the writer
#: (a regression to the old blocking path would wait ~the full hold and fail).
#: Widening the hold is FREE on the passing path — ``_HeldLock.__exit__`` releases
#: the lock the instant the (fast) read returns, so the full hold elapses ONLY on
#: the regression path (a blocked read waits out the whole window). A wider hold
#: therefore buys a larger pass/fail separation at zero healthy-run cost
#: (DEC-DASHBOARD-FASTREAD-FLAKE-001; widened again 2 s → 4 s by
#: DEC-DASHBOARD-FASTREAD-FLAKE-002).
_WRITER_HOLD_S: float = 4.0

#: A generous per-read ceiling — ``_WRITER_HOLD_S / 4`` (1.0 s). The discriminating
#: signal is "far below the writer's hold": a cache/in-memory read completes in
#: single-digit ms (~250x under this budget), while the old blocking path waits
#: ~``_WRITER_HOLD_S`` (4x OVER this budget → a clear, unambiguous fail). The prior
#: 0.2 s ceiling flaked on loaded CI runners (a 4.8 ms scheduling spike over 0.2 s
#: spuriously failed + blocked a merge — DEC-DASHBOARD-FASTREAD-FLAKE-001, widened
#: to 0.5 s); the 0.5 s ceiling then ALSO flaked once the suite grew to ~12.5k
#: tests (~17 min on a shared GHA runner): a 556.2 ms scheduling spike over 0.5 s
#: failed a cache-served read that a revert-proof blocked path would have held for
#: the FULL 2 s hold (PR #1093 run 28689390506). 1.0 s keeps the unambiguous 4x
#: regression separation with ~2x the jitter headroom on the healthy path
#: (DEC-DASHBOARD-FASTREAD-FLAKE-002).
_READ_BUDGET_S: float = 1.0


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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


def _write_spot_bar(lake_root: Path) -> None:
    """Write one real 1-minute NIFTY bar so the spot summary has content."""
    writer = ParquetWriter(lake_root=lake_root)
    bar = CandleBar(
        timestamp=datetime(2024, 1, 2, 3, 45, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=0,
    )
    writer.write(
        bars=[bar],
        exchange="NSE",
        segment="CASH",
        symbol="NIFTY",
        interval="1minute",
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[InstrumentStore]:
    lake_root = tmp_path / "lake"
    lake_root.mkdir()
    s = InstrumentStore.open(lake_root / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    _write_spot_bar(lake_root)
    yield s
    s.close()


@pytest.fixture
def client(store: InstrumentStore) -> Generator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c


def _time_get(client: TestClient, url: str) -> tuple[int, float]:
    """Return ``(status_code, elapsed_seconds)`` for a GET."""
    t0 = time.perf_counter()
    resp = client.get(url)
    return resp.status_code, time.perf_counter() - t0


class _HeldLock:
    """Context manager: hold the store's RLock on a background thread ~hold_s.

    Simulates the active writer (the ``_LockedReadConn`` acquires this exact
    lock). The lock is acquired BEFORE ``__enter__`` returns (a barrier) so the
    body runs strictly while the writer holds it.
    """

    def __init__(self, store: InstrumentStore, hold_s: float) -> None:
        self._store = store
        self._hold_s = hold_s
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        # Whitebox writer simulation: hold the SAME RLock the _LockedReadConn
        # acquires (private access is not flagged in tests; mirrors the existing
        # whitebox store suites).
        with self._store._lock:
            self._acquired.set()
            # Hold for the full window unless released early by __exit__.
            self._release.wait(timeout=self._hold_s)

    def __enter__(self) -> _HeldLock:
        self._thread.start()
        assert self._acquired.wait(timeout=5.0), "writer thread never took the lock"
        return self

    def __exit__(self, *_exc: object) -> None:
        self._release.set()
        self._thread.join(timeout=5.0)


def test_state_route_responsive_while_writer_holds_lock(
    client: TestClient, store: InstrumentStore
) -> None:
    """``/api/v1/state`` returns 200 fast even while the writer holds the lock.

    ``/api/v1/state`` is a pure in-memory snapshot — it must not be serialised
    behind the store lock. A fast 200 here proves the event loop stayed free
    (the old all-async-but-blocking layout would have stalled the loop behind any
    concurrent blocking read).
    """
    with _HeldLock(store, _WRITER_HOLD_S):
        status, elapsed = _time_get(client, "/api/v1/state")
    assert status == 200
    assert elapsed < _READ_BUDGET_S, (
        f"/api/v1/state took {elapsed * 1000:.1f}ms while the writer held the "
        f"lock for {_WRITER_HOLD_S}s — the event loop is blocking behind the "
        f"writer (regression)."
    )


def test_cache_served_coverage_spot_responsive_under_write(
    client: TestClient, store: InstrumentStore
) -> None:
    """A cache-served ``/api/v1/coverage/spot`` returns 200 fast under a write.

    Priming the route once populates the SummaryCache. A second poll within the
    2 s TTL floor is served from memory (O(1)) — it touches neither the lake nor
    the store lock — so it stays fast even while the writer holds the lock.
    """
    # Prime the cache (this first call does the real scan, before the writer).
    first = client.get("/api/v1/coverage/spot")
    assert first.status_code == 200
    assert first.json()["headline"]["datasets"] >= 1  # the NIFTY 1minute dataset

    with _HeldLock(store, _WRITER_HOLD_S):
        status, elapsed = _time_get(client, "/api/v1/coverage/spot")
    assert status == 200
    assert elapsed < _READ_BUDGET_S, (
        f"cache-served /api/v1/coverage/spot took {elapsed * 1000:.1f}ms while "
        f"the writer held the lock — it is not being served from the summary "
        f"cache (regression)."
    )


def test_both_reads_return_correct_payloads_unblocked(client: TestClient) -> None:
    """Sanity: with no writer holding the lock both reads return correct JSON."""
    state = client.get("/api/v1/state")
    assert state.status_code == 200
    assert "phases" in state.json()

    spot = client.get("/api/v1/coverage/spot")
    assert spot.status_code == 200
    body = spot.json()
    assert body["headline"]["datasets"] >= 1
    symbols = {row["symbol"] for row in body["rows"]}
    assert "NIFTY" in symbols
