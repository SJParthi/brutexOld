"""Unit tests for ``brutex.adapters.groww.spot_bounds`` (DEC-SPOT-1DAY-FIRST-001).

100% line+branch: the pure window math (``spot_coverage_to_minute_window``'s None /
start-moves / start-clamp / degenerate-start>=end branches), the lake-read
``spot_daily_coverage`` (over a REAL Parquet+DuckDB lake, including the cross-UTC-month
midnight-IST golden case), and the ``discover_spot_minute_windows`` reconcile-once
driver (per-symbol bound / no-coverage omit / per-symbol quarantine / reconcile=False /
multi-segment reconcile / catalog-unavailable degrade).

The lake tests build a real ``ParquetBarSink`` (``catalog=None`` — the dashboard/spot-pull
reality) + a real ``Catalog`` under ``tmp_path``, exactly mirroring the option twin's
blessed reconcile→read pattern (``test_groww_contract_bounds``). No network, no Groww SDK.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

from brutex.adapters.groww.contract_bounds import ContractCoverage
from brutex.adapters.groww.spot_bounds import (
    discover_spot_minute_windows,
    spot_coverage_to_minute_window,
    spot_daily_coverage,
)
from brutex.calendars.nse import IST
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

_FULL_START = date(2020, 1, 1)
_FULL_END = date(2026, 6, 18)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ─────────────────────────────────────────────────────────────────────────
def _index(
    symbol: str = "NIFTY",
    *,
    exchange: Exchange = Exchange.NSE,
    segment: Segment = Segment.INDEX,
) -> Instrument:
    """A minimal spot :class:`Instrument` keyed by *symbol* (the partition key)."""
    return Instrument(
        exch_id=exchange,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.IDX,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _daily_bar(day: date, *, clock: time = time(9, 15)) -> CandleBar:
    """One 1-day candle stamped at *clock* IST → stored UTC (the lake convention)."""
    ts = datetime.combine(day, clock, tzinfo=IST).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("1.00"),
        high=Decimal("2.00"),
        low=Decimal("0.50"),
        close=Decimal("1.50"),
        volume=10,
        open_interest=None,
    )


def _write_daily(
    lake_root: Path,
    instrument: Instrument,
    days: list[date],
    *,
    clock: time = time(9, 15),
) -> None:
    """Append *days*' 1-day candles for *instrument*'s spot partition (catalog-free sink)."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=instrument.exch_id.value,
        segment=instrument.segment.value,
        symbol=instrument.symbol_name,
        interval="1day",
        bars=[_daily_bar(d, clock=clock) for d in days],
    )


# ─────────────────────────────────────────────────────────────────────────
# spot_coverage_to_minute_window (pure)
# ─────────────────────────────────────────────────────────────────────────
def test_window_none_coverage_is_full_range() -> None:
    # Defensive: a None coverage maps to the full range (never-skip if it slips through).
    assert spot_coverage_to_minute_window(None, full_start=_FULL_START, full_end=_FULL_END) == (
        _FULL_START,
        _FULL_END,
    )


def test_window_start_moves_to_listing_end_stays_full_end() -> None:
    # The spot divergence: start = the discovered 1-day listing day; end = full_end
    # (a spot INDEX never expires, so the minute fill runs to the requested upper bound,
    # NOT clamped to the last daily bar — the option-only behaviour).
    cov = ContractCoverage(first_date=date(2022, 4, 5), last_date=date(2026, 6, 1))
    window = spot_coverage_to_minute_window(cov, full_start=_FULL_START, full_end=_FULL_END)
    assert window == (date(2022, 4, 5), _FULL_END)


def test_window_start_clamped_up_to_full_start() -> None:
    # Coverage starts BEFORE full_start → start clamps up to full_start (never widens).
    cov = ContractCoverage(first_date=date(2019, 6, 1), last_date=date(2026, 6, 1))
    window = spot_coverage_to_minute_window(cov, full_start=_FULL_START, full_end=_FULL_END)
    assert window == (_FULL_START, _FULL_END)


def test_window_degenerate_start_at_or_after_end_falls_back_to_full() -> None:
    # Coverage's listing day is AT/AFTER full_end (a symbol that began trading after the
    # requested window) → start >= end → the conservative never-skip full range.
    cov = ContractCoverage(first_date=date(2026, 7, 1), last_date=date(2026, 8, 1))
    window = spot_coverage_to_minute_window(cov, full_start=_FULL_START, full_end=_FULL_END)
    assert window == (_FULL_START, _FULL_END)


def test_window_listing_exactly_on_full_end_is_degenerate() -> None:
    # Boundary: listing == full_end → start == end → degenerate → full range (the
    # ``>=`` guard, not ``>``).
    cov = ContractCoverage(first_date=_FULL_END, last_date=_FULL_END)
    window = spot_coverage_to_minute_window(cov, full_start=_FULL_START, full_end=_FULL_END)
    assert window == (_FULL_START, _FULL_END)


# ─────────────────────────────────────────────────────────────────────────
# spot_daily_coverage (lake read)
# ─────────────────────────────────────────────────────────────────────────
def test_daily_coverage_none_when_no_partitions(tmp_path: Path) -> None:
    # Empty lake → reconcile finds nothing → no partitions → None.
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=Segment.INDEX.value, interval="1day"
        )
        assert spot_daily_coverage(catalog, instrument=_index()) is None


def test_daily_coverage_spans_min_first_to_max_last(tmp_path: Path) -> None:
    inst = _index("NIFTY")
    # Two months → two partitions; coverage = global min(first)..max(last).
    _write_daily(tmp_path, inst, [date(2022, 1, 3), date(2022, 1, 6), date(2022, 2, 3)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=Segment.INDEX.value, interval="1day"
        )
        cov = spot_daily_coverage(catalog, instrument=inst)
    assert cov == ContractCoverage(first_date=date(2022, 1, 3), last_date=date(2022, 2, 3))


def test_daily_coverage_cross_utc_month_midnight_ist_golden(tmp_path: Path) -> None:
    # GOLDEN: a midnight-IST 2025-01-01 bar is stored as 2024-12-31T18:30Z (the DECEMBER
    # UTC partition), yet the IST trading day is 2025-01-01. min(first_ts)/max(last_ts)
    # over ALL partitions + IST conversion must recover the IST dates, NOT the UTC dates.
    inst = _index("BANKNIFTY")
    _write_daily(tmp_path, inst, [date(2025, 1, 1), date(2025, 2, 1)], clock=time(0, 0))
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=Segment.INDEX.value, interval="1day"
        )
        cov = spot_daily_coverage(catalog, instrument=inst)
    assert cov == ContractCoverage(first_date=date(2025, 1, 1), last_date=date(2025, 2, 1))


# ─────────────────────────────────────────────────────────────────────────
# discover_spot_minute_windows (driver)
# ─────────────────────────────────────────────────────────────────────────
def test_discover_bounds_symbol_and_omits_no_coverage(tmp_path: Path) -> None:
    bounded = _index("NIFTY")  # has 1-day data → bounded window
    no_data = _index("NEWNIFTY")  # no lake data → OMITTED (the 3c rule)
    _write_daily(tmp_path, bounded, [date(2022, 4, 5), date(2022, 4, 20)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog, capture_logs() as logs:
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[bounded, no_data],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    # The bounded symbol's window starts at its discovered 1-day listing date; the end is
    # full_end (a spot index never expires). The no-coverage symbol is ABSENT.
    assert windows == {"NIFTY": (date(2022, 4, 5), _FULL_END)}
    summary = next(e for e in logs if e["event"] == "spot_bounds.derived")
    assert (summary["bounded"], summary["no_coverage"], summary["skipped"]) == (1, 1, 0)


def test_discover_bounds_reconcile_false_reads_prereconciled_catalog(tmp_path: Path) -> None:
    inst = _index("NIFTY")
    _write_daily(tmp_path, inst, [date(2022, 4, 5), date(2022, 4, 20)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=Segment.INDEX.value, interval="1day"
        )
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[inst],
            full_start=_FULL_START,
            full_end=_FULL_END,
            reconcile=False,
        )
    assert windows == {"NIFTY": (date(2022, 4, 5), _FULL_END)}


def test_discover_bounds_multi_segment_reconciles_each_once(tmp_path: Path) -> None:
    # Two DISTINCT spot segments (INDEX + CASH) → the driver reconciles each segment
    # exactly once (deterministic order), then reads both symbols' coverage.
    index_inst = _index("NIFTY", segment=Segment.INDEX)
    cash_inst = _index("RELIANCE", segment=Segment.CASH)
    _write_daily(tmp_path, index_inst, [date(2022, 1, 3)])
    _write_daily(tmp_path, cash_inst, [date(2023, 6, 1)])
    reconciled: list[str] = []
    real_reconcile = reconcile_catalog_from_lake

    def _spy(**kwargs: object) -> None:
        reconciled.append(str(kwargs["segment"]))
        real_reconcile(**kwargs)  # type: ignore[arg-type]

    import brutex.data.catalog_reconcile as cr

    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        # Patch the symbol the driver imports locally.
        original = cr.reconcile_catalog_from_lake
        cr.reconcile_catalog_from_lake = _spy  # type: ignore[assignment]
        try:
            windows = discover_spot_minute_windows(
                catalog,
                lake_root=tmp_path,
                instruments=[index_inst, cash_inst],
                full_start=_FULL_START,
                full_end=_FULL_END,
            )
        finally:
            cr.reconcile_catalog_from_lake = original  # type: ignore[assignment]
    # Reconciled once per distinct segment, in sorted (deterministic) order.
    assert reconciled == sorted({Segment.INDEX.value, Segment.CASH.value})
    assert windows == {
        "NIFTY": (date(2022, 1, 3), _FULL_END),
        "RELIANCE": (date(2023, 6, 1), _FULL_END),
    }


def test_discover_bounds_reconcile_unavailable_degrades_to_empty(tmp_path: Path) -> None:
    # Files present + a CLOSED catalog → reconcile's upsert raises CatalogError → the
    # whole pass degrades to {} (skip the minute pull) and logs LOUDLY.
    inst = _index("NIFTY")
    _write_daily(tmp_path, inst, [date(2022, 4, 5)])
    catalog = Catalog.open(tmp_path / "catalog.duckdb")
    catalog.close()
    with capture_logs() as logs:
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[inst],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    assert windows == {}
    assert any(e["event"] == "spot_bounds.reconcile_unavailable" for e in logs)


def test_discover_bounds_skips_on_mid_loop_catalog_error(tmp_path: Path) -> None:
    # reconcile=False + a closed catalog → the per-symbol list_partitions raises
    # CatalogError, caught per symbol (quarantine), pass returns {} + logs the skip.
    catalog = Catalog.open(tmp_path / "catalog.duckdb")
    catalog.close()
    with capture_logs() as logs:
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[_index("NIFTY")],
            full_start=_FULL_START,
            full_end=_FULL_END,
            reconcile=False,
        )
    assert windows == {}
    skipped = next(e for e in logs if e["event"] == "spot_bounds.symbol_skipped")
    assert skipped["symbol"] == "NIFTY"


def test_discover_bounds_empty_instruments_is_empty_map(tmp_path: Path) -> None:
    # No instruments → no segments to reconcile, an empty map, no crash.
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    assert windows == {}


def test_discover_bounds_degenerate_listing_after_end_uses_full_range(tmp_path: Path) -> None:
    # A symbol whose ONLY 1-day data is at/after the requested full_end → the pure mapper
    # returns the full range (never-skip), and the symbol IS in the map (it has coverage).
    inst = _index("NIFTY")
    _write_daily(tmp_path, inst, [date(2026, 7, 1)])  # after full_end
    narrow_end = date(2026, 6, 1)
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        windows = discover_spot_minute_windows(
            catalog,
            lake_root=tmp_path,
            instruments=[inst],
            full_start=_FULL_START,
            full_end=narrow_end,
        )
    assert windows == {"NIFTY": (_FULL_START, narrow_end)}


def test_module_exports() -> None:
    """The public surface is exactly the documented four names + the SpotWindows alias."""
    import brutex.adapters.groww.spot_bounds as mod

    assert set(mod.__all__) == {
        "SpotWindows",
        "discover_spot_minute_windows",
        "spot_coverage_to_minute_window",
        "spot_daily_coverage",
    }
