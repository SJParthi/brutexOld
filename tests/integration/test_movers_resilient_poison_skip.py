"""A dup-poisoned symbol on ``/movers`` is HEALED at read, never a 500.

History: DEC-LAKE-WRITE-UNIQUE-001 first bounded the blast radius by adding
the resampler tripwire's :class:`~brutex.data.errors.ResampleInputError`
(DEC-RESAMPLER-INPUT-GUARD-001) to
:func:`~brutex.dashboard.movers_resilient.movers_read_skip_errors`, so ONE
poisoned symbol was SKIPPED (counted) instead of 500ing the page. Dup-train
PR-2 (DEC-LAKE-READ-UNIQUE-001) then wired the keep-FIRST read gate into the
movers day readers, so a duplicate-only poison is now COLLAPSED before the
tripwire ever sees it — the symbol RANKS with its real bars + one aggregated
``lake.read.duplicates_dropped`` WARNING. The tripwire's skip-set membership
stays pinned here as the guard for any poisoned path that bypasses the day
readers, and the corrupt-Parquet skip of DEC-MOVERS-CORRUPT-SKIP-001 is
unchanged.

Also pins the set's honest boundary: :class:`~brutex.data.errors.LakeWriteHalt`
(a sibling ``DataLakeError`` — a pull-wide stop) is NOT in the skip set, so a
disk-full/OOM can never be miscounted as a "skipped symbol".

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_resilient import movers_read_skip_errors
from brutex.dashboard.movers_snapshot import compute_spot_candidates
from brutex.data.errors import LakeWriteHalt, ResampleInputError
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_DAY = date(2024, 6, 13)


def _min_bar(minute: int, *, close: str, volume: int) -> CandleBar:
    """A flat 1-minute bar at 09:35+*minute* IST (04:05+*minute* UTC) on ``_DAY``."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(_DAY.year, _DAY.month, _DAY.day, 4, 5 + minute, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


def _poison_1minute_partition(
    lake_root: Path, *, exchange: str, symbol: str, rows: Sequence[tuple[int, float]]
) -> None:
    """Write a RAW pyarrow 1-minute partition carrying DUPLICATE timestamps.

    ``rows`` are ``(minute_offset, price)`` — repeated offsets are written
    verbatim, simulating a partition poisoned before DEC-LAKE-WRITE-UNIQUE-001
    (the fixed writer itself now refuses to store duplicates, so the poison
    must be planted with raw pyarrow, exactly like the operator's lake).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest = lake_root / "bars" / exchange / "INDEX" / symbol / "1minute" / "2024" / "06.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamps = [
        datetime(_DAY.year, _DAY.month, _DAY.day, 4, 5 + offset, tzinfo=UTC) for offset, _ in rows
    ]
    prices = [price for _, price in rows]
    table = pa.table(
        {
            "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _seed_good_and_poisoned(lake_root: Path) -> None:
    """A healthy NIFTY 1-minute series + a dup-poisoned SENSEX 1-minute series."""
    ParquetWriter(lake_root=lake_root).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_min_bar(0, close="22000", volume=500), _min_bar(1, close="22010", volume=400)],
    )
    # Duplicate minute-0 rows → the resampler tripwire raises ResampleInputError.
    _poison_1minute_partition(
        lake_root,
        exchange="BSE",
        symbol="SENSEX",
        rows=[(0, 77000.0), (0, 77000.0), (1, 77010.0)],
    )


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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


class TestSkipSetMembership:
    def test_resample_input_error_is_in_the_movers_skip_set(self) -> None:
        """FAILS on unfixed main: ResampleInputError was not in the set."""
        assert ResampleInputError in movers_read_skip_errors()

    def test_lake_write_halt_is_not_swallowed_by_the_skip_set(self) -> None:
        """The set stays narrow: a pull-wide LakeWriteHalt is never a 'skipped symbol'."""
        skip_set = movers_read_skip_errors()
        assert LakeWriteHalt not in skip_set
        assert not issubclass(LakeWriteHalt, skip_set)


class TestPoisonedSymbolSkippedNot500:
    def test_compute_heals_the_poisoned_symbol_keep_first(self, tmp_path: Path) -> None:
        """The dup-poisoned symbol now RANKS with its deduped bars + a WARNING.

        DEC-LAKE-READ-UNIQUE-001 supersedes the whole-symbol skip for a
        duplicate-only poison: the read gate collapses the duplicate keep-FIRST
        BEFORE the resampler tripwire, so the symbol serves its REAL bars
        (warned, never silent) instead of vanishing from the screen. The
        tripwire + skip set remain the guard for a genuinely CORRUPT file.
        (Before the read gate this same fixture pinned skipped == 1.)
        """
        from structlog.testing import capture_logs

        _seed_good_and_poisoned(tmp_path)
        with capture_logs() as logs:
            result = compute_spot_candidates(
                lake_root=tmp_path, interval="5minute", date="2024-06-13", time=None, exchange=None
            )
        # BOTH symbols rank — the duplicate was healed at read, nothing skipped.
        assert {s.instrument.symbol for s in result.snapshots} == {"NIFTY", "SENSEX"}
        assert result.skipped == 0
        # ONE aggregated warning PER READ (the screen makes several bounded reads
        # of the same poisoned month — session + prev-day probes), every one
        # naming the poisoned SENSEX partition, never a per-row storm.
        dedups = [e for e in logs if e["event"] == "lake.read.duplicates_dropped"]
        assert dedups, "the collapse must be warned, never silent"
        assert all("SENSEX" in str(e["source"]) for e in dedups)
        assert all(e["dropped"] == 1 for e in dedups)

    def test_movers_route_returns_200_with_poisoned_symbol(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """FAILS on unfixed main: the route 500'd on the poisoned symbol."""
        _seed_good_and_poisoned(tmp_path)
        client = TestClient(
            build_app(DashboardState(), instrument_store=store),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/api/v1/movers",
            params={
                "asset": "spot",
                "interval": "5minute",
                "date": "2024-06-13",
                "top_n": 5,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # The poisoned SENSEX is healed at read (keep-FIRST) and ranks alongside
        # NIFTY — no skip, no 500 (DEC-LAKE-READ-UNIQUE-001).
        assert {r["symbol"] for r in body["volume"]} == {"NIFTY", "SENSEX"}
        assert body["skipped_count"] == 0
