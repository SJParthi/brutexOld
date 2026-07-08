"""Integration regression: the F&O Universe Explorer over a REAL InstrumentStore.

The original ``brutex dashboard`` wiring passed the bare ``lake_root`` Path
straight into ``build_app`` as the "store", so every universe route 500'd with
``'PosixPath' object has no attribute 'load_snapshot'`` (cli.py now opens a real
:class:`~brutex.data.instrument_store.InstrumentStore` instead).

These tests exercise the exact failing operation — ``store.load_snapshot(...)``
on a freshly-opened (empty) store — feeding the coverage-tested query helpers,
with NO FastAPI dependency, so they run in every environment:

* an empty lake yields a valid empty universe (0 index / 0 stock), not a crash;
* a seeded index + F&O-eligible stock classify + count correctly through the
  same snapshot the route consumes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import build_fno_eligible_underlyings


def _open_empty(tmp_path: Path) -> InstrumentStore:
    """Open a fresh InstrumentStore (schema auto-created, zero rows)."""
    return InstrumentStore.open(tmp_path / "instruments.duckdb")


def test_empty_store_snapshot_yields_zero_universe(tmp_path: Path) -> None:
    """Regression for the load_snapshot 500: an empty opened store → 0/0, no crash."""
    store = _open_empty(tmp_path)
    snapshot = store.load_snapshot(in_scope_only=False)  # the op that 500'd on a Path
    assert universe_summary(snapshot) == {"index_count": 0, "stock_count": 0, "total": 0}
    assert list_underlyings(snapshot, kind="index") == []
    assert list_underlyings(snapshot, kind="stock") == []


def _idx(symbol: str, name: str) -> Instrument:
    """Build a minimal NSE index (IDX) instrument-master row."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=name,
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


def test_seeded_indices_appear_in_universe(tmp_path: Path) -> None:
    """A seeded NSE index (incl. a non-F&O sectoral one) classifies as 'index'."""
    store = _open_empty(tmp_path)
    today = date.today()
    store.upsert_many(
        [_idx("NIFTY", "NIFTY 50"), _idx("NIFTYIT", "NIFTY IT")],
        snapshot_date=today,
        run_id="test-seed",
        csv_source_sha256="0" * 64,
    )
    snapshot = store.load_snapshot(in_scope_only=False)
    summary = universe_summary(snapshot)
    assert summary["index_count"] == 2  # broad-market NIFTY + sectoral NIFTY IT
    assert summary["stock_count"] == 0
    syms = {row["symbol"] for row in list_underlyings(snapshot, kind="index")}
    assert syms == {"NIFTY", "NIFTYIT"}


def _eq(symbol: str) -> Instrument:
    """Build a minimal NSE cash-equity (EQ) instrument-master row."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=hash(symbol) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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


def _ce(underlying: str, n: int) -> Instrument:
    """Build a minimal NSE CE option leg naming *underlying*."""
    return Instrument(
        exch_id=Exchange.NSE,
        # Unique per (underlying, n) leg — the new PK is
        # (security_id, exch_id, segment, snapshot_date), so two legs sharing
        # only ``n`` (e.g. RELIANCE-1 vs TCS-1) must NOT collide on security_id.
        security_id=500_000 + abs(hash(f"{underlying}CE{n}")) % 400_000,
        symbol_name=f"{underlying}CE{n}",
        display_name=f"{underlying} CE",
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=hash(underlying) % 100_000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 1, 30),
        strike_price=Decimal(str(20000 + n)),
        option_type="CE",
        asm_gsm_flag=False,
    )


def test_fno_eligible_sql_equals_pure_rule(tmp_path: Path) -> None:
    """Property: store.fno_eligible_underlyings() == build_fno_eligible_underlyings()
    on the same FULL (with-legs) data — pins the SQL fast-path equal to the
    single-source pure rule (DEC-INSTRUMENT-UNIVERSE-FAST-001)."""
    store = _open_empty(tmp_path)
    full = [
        _idx("NIFTY", "NIFTY 50"),
        _eq("RELIANCE"),
        _eq("TCS"),
        _ce("RELIANCE", 1),
        _ce("RELIANCE", 2),
        _ce("TCS", 1),
    ]
    store.upsert_many(
        full,
        snapshot_date=date.today(),
        run_id="test-full",
        csv_source_sha256="0" * 64,
    )
    assert store.fno_eligible_underlyings() == build_fno_eligible_underlyings(full)
