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

from brutex.adapters.groww.instruments import InstrumentMasterFetcher
from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.data.instrument_store import Instrument, InstrumentStore
from brutex.models import Exchange, InstrumentType, Segment
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
        exchange=Exchange.NSE,
        exchange_token=hash(symbol) % 100_000,
        trading_symbol=symbol,
        vendor_symbol=f"NSE-{symbol}",
        name=name,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
        internal_trading_symbol=None,
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
        exchange=Exchange.NSE,
        exchange_token=hash(symbol) % 100_000,
        trading_symbol=symbol,
        vendor_symbol=f"NSE-{symbol}",
        name=symbol,
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
        internal_trading_symbol=None,
    )


def _ce(underlying: str, n: int) -> Instrument:
    """Build a minimal NSE CE option leg naming *underlying*."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=500_000 + n,
        trading_symbol=f"{underlying}CE{n}",
        vendor_symbol=f"NSE-{underlying}-30Jan25-{n}-CE",
        name=f"{underlying} CE",
        instrument_type=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=hash(underlying) % 100_000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        freeze_quantity=1800,
        expiry_date=date(2025, 1, 30),
        strike_price=Decimal(str(20000 + n)),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
        internal_trading_symbol=None,
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


def test_lean_ingest_persists_only_underlyings(tmp_path: Path) -> None:
    """Measure the win: ~30 IDX + ~211 EQ + ~89k CE legs → only ~241 stored rows.

    Builds a synthetic master CSV, refreshes it through the REAL lean
    InstrumentMasterFetcher into a REAL store, and asserts the instruments
    table holds only the universe underlyings (legs dropped) and that the
    fast /universe path returns the right counts (DEC-INSTRUMENT-UNIVERSE-FAST-001).
    """
    n_idx, n_eq, n_legs = 30, 211, 89_000
    header = (
        "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,"
        "series,isin,underlying_symbol,underlying_exchange_token,expiry_date,strike_price,"
        "lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed"
    )
    rows: list[str] = []
    for i in range(n_idx):
        rows.append(f"NSE,{1000 + i},IDX{i},NSE-IDX{i},Index {i},IDX,CASH,,,,,,,1,0.05,,0,1,1")
    for i in range(n_eq):
        rows.append(
            f"NSE,{20000 + i},EQ{i},NSE-EQ{i},Equity {i},EQ,CASH,EQ,INE{i:09d},,,,,1,0.05,,0,1,1"
        )
    # 89k CE legs spread across the EQ underlyings → every EQ is F&O-eligible.
    for j in range(n_legs):
        u = f"EQ{j % n_eq}"
        rows.append(
            f"NSE,{500000 + j},{u}CE{j},NSE-{u}-30Jan25-{j}-CE,{u} CE,CE,FNO,,,{u},"
            f"{20000 + (j % n_eq)},2025-01-30,{20000 + j},75,0.05,1800,0,1,1"
        )
    csv_text = "\n".join([header, *rows]) + "\n"

    store = _open_empty(tmp_path)
    fetcher = InstrumentMasterFetcher(
        http_get=lambda _url: csv_text,
        store=store,
        run_id_factory=lambda: "lean-measure",
    )
    result = fetcher.refresh(snapshot_date=date.today())

    # Parsed everything, persisted only the ~241 underlyings, dropped the legs.
    assert result.parsed == n_idx + n_eq + n_legs
    assert result.persisted == n_idx + n_eq  # 241
    assert result.legs_dropped == n_legs
    # The instruments TABLE physically holds only the 241 underlyings.
    assert store.count(in_scope_only=False) == n_idx + n_eq
    # The fast /universe path returns the right counts off the lean store.
    eligible = store.fno_eligible_underlyings()
    candidates = store.list_index_equity_instruments()
    assert len(candidates) == n_idx + n_eq
    summary = universe_summary(candidates, fno_eligible=eligible)
    assert summary == {"index_count": n_idx, "stock_count": n_eq, "total": n_idx + n_eq}
