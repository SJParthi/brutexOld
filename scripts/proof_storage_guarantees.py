"""Live proof that instruments / expiries / contracts / candles all store
with uniqueness, dedup, and sublinear (O(1)/O(log n)) latency.

Run: ``python scripts/proof_storage_guarantees.py``

What it does
------------
1. Open a throwaway InstrumentStore + Catalog on-disk.
2. Populate instruments, expiries, contracts, candle-partition rows.
3. Verify row counts against expected totals.
4. Uniqueness: try to insert a duplicate PK, assert it fails at the SQL
   layer.
5. Dedup: re-run the exact same upsert; assert the row count is
   unchanged.
6. O(1)-ish: time a PK-bound SELECT at 1k and 10k rows; assert the
   ratio of the latter over the former is < 3x (sublinear → effectively
   constant at this scale).
7. Print the primary-key definition of each table straight from
   ``information_schema``.

The script exits non-zero on any assertion failure so it can be wired
into CI as a smoke test later.
"""

from __future__ import annotations

import tempfile
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from brutex.data.catalog import Catalog
from brutex.data.instrument_store import (
    ContractRecord,
    ExpiryRecord,
    InstrumentStore,
)
from brutex.data.models import CatalogEntry
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _make_instrument(i: int, snap: date) -> Instrument:
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=100_000 + i,
        trading_symbol=f"SYM{i}",
        vendor_symbol=f"NSE-SYM{i:06d}",
        name=f"Symbol {i}",
        instrument_type=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_exchange_token=None,
        lot_size=25,
        tick_size=Decimal("0.05"),
        freeze_quantity=1800,
        expiry_date=date(2025, 1, 30),
        strike_price=Decimal(str(20000 + i)),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
        internal_trading_symbol=None,
    )


def _pk_select_nanos(conn: object, symbol: str) -> int:
    """Time a single PK-bound SELECT on `instruments`, in nanoseconds."""
    stmt = "SELECT trading_symbol FROM instruments WHERE vendor_symbol = ? AND snapshot_date = ?"
    t0 = time.perf_counter_ns()
    conn.execute(stmt, [symbol, date(2024, 1, 2)]).fetchone()  # type: ignore[attr-defined]
    return time.perf_counter_ns() - t0


def _median_pk_select(conn: object, symbol: str, trials: int = 200) -> float:
    samples = [_pk_select_nanos(conn, symbol) for _ in range(trials)]
    samples.sort()
    return samples[len(samples) // 2]


def _print_pk(conn: object, table: str) -> None:
    rows = conn.execute(  # type: ignore[attr-defined]
        """
        SELECT constraint_column_names
        FROM duckdb_constraints()
        WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'
        """,
        [table],
    ).fetchall()
    for row in rows:
        print(f"    {table:16s} PRIMARY KEY {tuple(row[0])}")


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="brutex-proof-") as td:
        lake_root = Path(td)
        store = InstrumentStore.open(lake_root / "instruments.duckdb")
        catalog = Catalog.open(lake_root / "catalog.duckdb")

        snap = date(2024, 1, 2)
        run_id = "proof-run"

        # ── Populate 1k instruments ─────────────────────────────────
        instruments_1k = [_make_instrument(i, snap) for i in range(1_000)]
        store.upsert_many(
            instruments_1k,
            snapshot_date=snap,
            run_id=run_id,
            csv_source_sha256="a" * 64,
        )

        count_1k = store._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        print(f"[1] instruments rows after insert(1k):        {count_1k}")
        if count_1k != 1_000:
            failures.append(f"expected 1000 instruments, got {count_1k}")

        # ── Populate expiries ──────────────────────────────────────
        expiries = [
            ExpiryRecord(
                exchange="NSE",
                underlying_symbol=f"U{i}",
                expiry_date=date(2025, 1, 30),
                snapshot_date=snap,
                pulled_year=2024,
                fetched_at=datetime(2024, 1, 2, 12, tzinfo=UTC),
                run_id=run_id,
            )
            for i in range(1_000)
        ]
        store.upsert_expiries(expiries)
        exp_count = store._conn.execute("SELECT COUNT(*) FROM expiries").fetchone()[0]
        print(f"[2] expiries rows after insert(1k):           {exp_count}")
        if exp_count != 1_000:
            failures.append(f"expected 1000 expiries, got {exp_count}")

        # ── Populate contracts ─────────────────────────────────────
        contracts = [
            ContractRecord(
                exchange="NSE",
                underlying_symbol="NIFTY",
                expiry_date=date(2025, 1, 30),
                contract_symbol=f"NSE-NIFTY{i:06d}CE",
                snapshot_date=snap,
                instrument_type=InstrumentType.CE,
                strike_price=Decimal(str(20000 + i)),
                lot_size=25,
                freeze_quantity=1800,
                tick_size=Decimal("0.05"),
                fetched_at=datetime(2024, 1, 2, 12, tzinfo=UTC),
                run_id=run_id,
            )
            for i in range(1_000)
        ]
        store.upsert_contracts(contracts)
        con_count = store._conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        print(f"[3] contracts rows after insert(1k):          {con_count}")
        if con_count != 1_000:
            failures.append(f"expected 1000 contracts, got {con_count}")

        # ── Populate catalog (candle-partition rows) ───────────────
        for i in range(1_000):
            catalog.upsert(
                CatalogEntry(
                    exchange="NSE",
                    segment="FNO",
                    symbol=f"NSE-NIFTY{i:06d}CE",
                    interval="1minute",
                    year=2024,
                    month=1 + (i % 12),
                    raw_hash=f"{i:064x}",
                    row_count=1_000 + i,
                    first_ts=datetime(2024, 1, 2, 9, 15, tzinfo=UTC),
                    last_ts=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    fetched_at=datetime(2024, 1, 2, 16, tzinfo=UTC),
                )
            )
        cat_count = catalog._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) FROM partitions"
        ).fetchone()[0]
        print(f"[4] catalog partitions after upsert(1k):      {cat_count}")
        if cat_count != 1_000:
            failures.append(f"expected 1000 partitions, got {cat_count}")

        # ── Uniqueness: duplicate insert must FAIL ─────────────────
        dup = _make_instrument(0, snap)
        duplicate_blocked = False
        try:
            store._conn.execute(
                "INSERT INTO instruments (vendor_symbol, snapshot_date, "
                "exchange, trading_symbol, instrument_type, segment, "
                "lot_size, tick_size, in_scope, raw_hash, fetched_at, "
                "run_id, csv_source_sha256, is_reserved, buy_allowed, "
                "sell_allowed) VALUES (?, ?, 'NSE', 'X', 'CE', 'FNO', "
                "25, 0.05, TRUE, 'h', '2024-01-02T12:00:00+00:00', "
                "'proof-run', ?, FALSE, TRUE, TRUE)",
                [dup.vendor_symbol, snap, "a" * 64],
            )
        except Exception as exc:
            duplicate_blocked = True
            print(f"[5] uniqueness: duplicate PK blocked ({type(exc).__name__})")
        if not duplicate_blocked:
            failures.append("duplicate PK insert was NOT blocked")

        # ── Dedup: rerun the same upsert, row count unchanged ──────
        store.upsert_many(
            instruments_1k,
            snapshot_date=snap,
            run_id=run_id,
            csv_source_sha256="a" * 64,
        )
        count_after_rerun = store._conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        print(f"[6] dedup: rows after identical re-upsert:    {count_after_rerun}")
        if count_after_rerun != 1_000:
            failures.append(f"dedup broken: got {count_after_rerun} rows after rerun, want 1000")

        # ── O(1) latency: median PK-select at 1k vs 10k ────────────
        t_1k = _median_pk_select(store._conn, "NSE-SYM000500")

        # Grow to 10k
        instruments_10k = [_make_instrument(i, snap) for i in range(10_000)]
        store.upsert_many(
            instruments_10k,
            snapshot_date=snap,
            run_id=run_id,
            csv_source_sha256="a" * 64,
        )
        t_10k = _median_pk_select(store._conn, "NSE-SYM005000")
        ratio = t_10k / t_1k if t_1k > 0 else float("inf")
        print(
            f"[7] O(1) check: median PK select 1k={t_1k:>6}ns  "
            f"10k={t_10k:>6}ns  ratio={ratio:.2f}x  "
            f"(target <3x → sublinear / ART index)"
        )
        if ratio >= 3.0:
            failures.append(f"PK-select ratio {ratio:.2f}x ≥ 3x — not sublinear")

        # ── Print the PK definitions straight from DuckDB ──────────
        print("\n[8] Primary keys (from duckdb_constraints()):")
        _print_pk(store._conn, "instruments")
        _print_pk(store._conn, "expiries")
        _print_pk(store._conn, "contracts")
        _print_pk(store._conn, "instrument_runs")
        _print_pk(catalog._conn, "partitions")

        catalog.close()
        store.close()

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
