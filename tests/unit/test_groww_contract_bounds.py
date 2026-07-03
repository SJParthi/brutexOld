"""Unit tests for ``brutex.adapters.groww.contract_bounds`` (DEC-DERIV-MINUTE-BOUNDS-001).

100% line+branch: the pure window math (``ContractCoverage`` invariant +
``coverage_to_minute_window``'s None / expiry-ceiling / last_date-fallback / clamp /
disjoint branches) and the lake-read path (``contract_daily_coverage`` over a REAL
Parquet+DuckDB lake — the cross-UTC-month midnight-IST golden case included — plus
``derive_contract_minute_windows``' reconcile-once driver, its per-contract omit /
disjoint / quarantine fallbacks, and the catalog-unavailable degrade).

The lake tests build a real ``ParquetBarSink`` (``catalog=None`` — the
dashboard/derivative-pull reality) + a real ``Catalog`` under ``tmp_path``, exactly
mirroring the blessed reconcile→read pattern (``cascade_wire`` /
``test_data_lake_real``). No network, no Groww SDK.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.groww.constants import OPTION_SEGMENT, TRACK1_DAILY_INTERVAL
from brutex.adapters.groww.contract_bounds import (
    DEFAULT_FNO_LOOKBACK_DAYS,
    DEFAULT_FUTURES_LOOKBACK_DAYS,
    DEFAULT_OPTION_LOOKBACK_DAYS,
    ContractCoverage,
    contract_daily_coverage,
    coverage_to_minute_window,
    default_fno_minute_window,
    derive_contract_minute_windows,
    fno_window_opens_on,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.calendars.nse import IST
from brutex.data.catalog import Catalog
from brutex.data.catalog_reconcile import reconcile_catalog_from_lake
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from structlog.testing import capture_logs

_FULL_START = date(2021, 1, 1)
_FULL_END = date(2026, 6, 13)


# ─────────────────────────────────────────────────────────────────────────
# Fixtures / builders
# ─────────────────────────────────────────────────────────────────────────
def _contract(
    symbol: str = "NSE-NIFTY-30Jan25-23000-CE",
    *,
    expiry: date | None = date(2025, 1, 30),
    contract_type: str = "CE",
    strike: float | None = 23000.0,
) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=expiry,
        symbol=symbol,
        contract_type=contract_type,
        strike=strike,
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
        open_interest=5,
    )


def _write_daily(
    lake_root: Path,
    symbol: str,
    days: list[date],
    *,
    exchange: str = "NSE",
    clock: time = time(9, 15),
) -> None:
    """Append *days*' 1-day candles for *symbol* under ``FNO`` (catalog-free sink)."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=exchange,
        segment=OPTION_SEGMENT,
        symbol=symbol,
        interval=TRACK1_DAILY_INTERVAL,
        bars=[_daily_bar(d, clock=clock) for d in days],
    )


# ─────────────────────────────────────────────────────────────────────────
# ContractCoverage (pure)
# ─────────────────────────────────────────────────────────────────────────
def test_contract_coverage_valid() -> None:
    cov = ContractCoverage(first_date=date(2024, 1, 2), last_date=date(2024, 1, 30))
    assert cov.first_date == date(2024, 1, 2)
    assert cov.last_date == date(2024, 1, 30)


def test_contract_coverage_single_day_is_valid() -> None:
    cov = ContractCoverage(first_date=date(2024, 1, 2), last_date=date(2024, 1, 2))
    assert cov.first_date == cov.last_date


def test_contract_coverage_inverted_raises() -> None:
    with pytest.raises(ValueError, match="precedes first_date"):
        ContractCoverage(first_date=date(2024, 1, 30), last_date=date(2024, 1, 2))


# ─────────────────────────────────────────────────────────────────────────
# coverage_to_minute_window (pure)
# ─────────────────────────────────────────────────────────────────────────
def test_window_none_coverage_is_full_range() -> None:
    assert coverage_to_minute_window(None, full_start=_FULL_START, full_end=_FULL_END) == (
        _FULL_START,
        _FULL_END,
    )


def test_window_uses_expiry_plus_one_as_end_ceiling() -> None:
    # Expired contract: start=listing (first_date), end=expiry+1 (end-exclusive).
    cov = ContractCoverage(first_date=date(2024, 1, 2), last_date=date(2024, 1, 26))
    window = coverage_to_minute_window(
        cov, full_start=_FULL_START, full_end=_FULL_END, expiry=date(2024, 1, 30)
    )
    assert window == (date(2024, 1, 2), date(2024, 1, 31))


def test_window_falls_back_to_last_date_when_no_expiry() -> None:
    cov = ContractCoverage(first_date=date(2024, 1, 2), last_date=date(2024, 1, 26))
    window = coverage_to_minute_window(cov, full_start=_FULL_START, full_end=_FULL_END, expiry=None)
    assert window == (date(2024, 1, 2), date(2024, 1, 27))


def test_window_live_contract_end_clamped_to_full_end() -> None:
    # Live contract: expiry in the future → end clamped to full_end (captures today's
    # edge; the stale daily last_date would have truncated it).
    cov = ContractCoverage(first_date=date(2026, 1, 1), last_date=date(2026, 6, 1))
    window = coverage_to_minute_window(
        cov, full_start=_FULL_START, full_end=_FULL_END, expiry=date(2026, 12, 25)
    )
    assert window == (date(2026, 1, 1), _FULL_END)


def test_window_start_clamped_to_full_start() -> None:
    # Coverage starts before full_start AND ends within range → start clamps up.
    cov = ContractCoverage(first_date=date(2020, 6, 1), last_date=date(2021, 3, 1))
    window = coverage_to_minute_window(
        cov, full_start=_FULL_START, full_end=_FULL_END, expiry=date(2021, 3, 5)
    )
    assert window == (_FULL_START, date(2021, 3, 6))


def test_window_disjoint_before_range_falls_back_to_full() -> None:
    # Coverage entirely before full_start → start>=end → conservative full range.
    cov = ContractCoverage(first_date=date(2020, 6, 1), last_date=date(2020, 12, 1))
    window = coverage_to_minute_window(
        cov, full_start=_FULL_START, full_end=_FULL_END, expiry=date(2020, 12, 5)
    )
    assert window == (_FULL_START, _FULL_END)


def test_window_disjoint_after_range_falls_back_to_full() -> None:
    # Coverage entirely after full_end → start>=end → conservative full range.
    cov = ContractCoverage(first_date=date(2027, 1, 1), last_date=date(2027, 2, 1))
    window = coverage_to_minute_window(
        cov, full_start=_FULL_START, full_end=_FULL_END, expiry=date(2027, 1, 30)
    )
    assert window == (_FULL_START, _FULL_END)


# ─────────────────────────────────────────────────────────────────────────
# default_fno_minute_window (pure — DEC-1MINUTE-ONLY-DERIVE-1DAY-001 /
# DEC-FUTURES-LOOKBACK-60D-001: FUTURES default 60 days, OPTIONS default 30 days)
# ─────────────────────────────────────────────────────────────────────────
def test_default_option_lookback_is_30_days() -> None:
    assert DEFAULT_OPTION_LOOKBACK_DAYS == 30


def test_default_futures_lookback_is_60_days() -> None:
    assert DEFAULT_FUTURES_LOOKBACK_DAYS == 60


def test_default_fno_lookback_legacy_alias_equals_option() -> None:
    # The legacy ``DEFAULT_FNO_LOOKBACK_DAYS`` name is kept as the OPTION value (30).
    assert DEFAULT_FNO_LOOKBACK_DAYS == DEFAULT_OPTION_LOOKBACK_DAYS == 30


def test_default_future_window_uses_expiry_minus_60_days() -> None:
    # A FUTURE (is_option=False) defaults to a 60-day lookback.
    win = default_fno_minute_window(
        date(2024, 1, 25),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 6, 13),
        is_option=False,
    )
    # [expiry−60 days, expiry+1day) — end-exclusive includes the expiry day's intraday
    # bars. 2024-01-25 − 60 days = 2023-11-26; expiry+1 day = 2024-01-26.
    assert win == (date(2023, 11, 26), date(2024, 1, 26))


def test_default_option_window_uses_expiry_minus_30_days() -> None:
    # An OPTION (is_option=True) stays on the tighter 30-day lookback — unchanged.
    win = default_fno_minute_window(
        date(2024, 1, 25),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 6, 13),
        is_option=True,
    )
    # 2024-01-25 − 30 days = 2023-12-26; expiry+1 day = 2024-01-26.
    assert win == (date(2023, 12, 26), date(2024, 1, 26))


def test_default_future_window_clamps_start_to_full_start() -> None:
    # expiry−60 days (2019-12-17) is BEFORE the floor → start clamps up to full_start.
    win = default_fno_minute_window(
        date(2020, 2, 15),
        full_start=date(2020, 1, 20),
        full_end=date(2026, 6, 13),
        is_option=False,
    )
    assert win == (date(2020, 1, 20), date(2020, 2, 16))


def test_default_option_window_clamps_start_to_full_start() -> None:
    # expiry−30 days (2020-01-16) is BEFORE the floor → start clamps up to full_start.
    win = default_fno_minute_window(
        date(2020, 2, 15),
        full_start=date(2020, 1, 20),
        full_end=date(2026, 6, 13),
        is_option=True,
    )
    assert win == (date(2020, 1, 20), date(2020, 2, 16))


def test_default_future_window_clamps_end_to_full_end() -> None:
    # expiry+1day is AFTER full_end → end clamps down to full_end.
    win = default_fno_minute_window(
        date(2026, 6, 30),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 6, 13),
        is_option=False,
    )
    # expiry−60 days = 2026-05-01; end clamps to full_end 2026-06-13.
    assert win == (date(2026, 5, 1), date(2026, 6, 13))


def test_default_fno_window_none_expiry_is_full_range() -> None:
    # The None-expiry guard fires regardless of is_option (both branches return full).
    full = (date(2020, 1, 1), date(2026, 6, 13))
    assert (
        default_fno_minute_window(None, full_start=full[0], full_end=full[1], is_option=False)
        == full
    )
    assert (
        default_fno_minute_window(None, full_start=full[0], full_end=full[1], is_option=True)
        == full
    )


def test_default_future_window_collapsed_span_falls_back_to_full() -> None:
    # expiry BEFORE full_start → the 60-day lookback window is entirely out of range
    # (start>=end) → conservative full range.
    full = (date(2024, 1, 1), date(2026, 6, 13))
    win = default_fno_minute_window(
        date(2023, 6, 1), full_start=full[0], full_end=full[1], is_option=False
    )
    assert win == full


def test_default_option_window_collapsed_span_falls_back_to_full() -> None:
    # The same empty-span guard for the 30-day option branch.
    full = (date(2024, 1, 1), date(2026, 6, 13))
    win = default_fno_minute_window(
        date(2023, 6, 1), full_start=full[0], full_end=full[1], is_option=True
    )
    assert win == full


# ─────────────────────────────────────────────────────────────────────────
# default_fno_minute_window — NOT-YET-OPEN skip (DEC-FNO-FAR-EXPIRY-WINDOW-001,
# operator law: STRICTLY [expiry − lookback, expiry] ∩ envelope — never a
# substitute window; not open yet ⇒ None ⇒ fetch NOTHING)
# ─────────────────────────────────────────────────────────────────────────
def test_default_far_expiry_option_not_open_returns_none() -> None:
    # REGRESSION (fails pre-fix): a Sep-2026 monthly option pulled on 2026-07-02
    # (full_end = prev-day 2026-07-01) has expiry−30d = 2026-08-30 AFTER full_end —
    # its strict window has not OPENED, so the answer is None (skip: zero vendor
    # calls). The OLD code degraded to the FULL 2020→today range (80 × 30-day
    # chunks — a measured 22.5% of a real ADANIENT pull wasted).
    win = default_fno_minute_window(
        date(2026, 9, 29),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 7, 1),
        is_option=True,
    )
    assert win is None


def test_default_far_expiry_future_not_open_returns_none() -> None:
    # REGRESSION (fails pre-fix) — the FUT twin: expiry−60d = 2026-07-31 is AFTER
    # full_end → not open yet → None (skip), never the full range.
    win = default_fno_minute_window(
        date(2026, 9, 29),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 7, 1),
        is_option=False,
    )
    assert win is None


def test_default_far_expiry_boundary_lookback_start_equals_full_end() -> None:
    # BOUNDARY (fails pre-fix): expiry−30d == full_end EXACTLY — the half-open
    # strict window [full_end, full_end) is still empty, so the contract opens
    # NEXT day: today's answer is None (the old code degraded to the full range).
    win = default_fno_minute_window(
        date(2026, 7, 31),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 7, 1),
        is_option=True,
    )
    assert win is None


def test_default_near_expiry_just_inside_full_end_unchanged() -> None:
    # expiry−30d == full_end − 1 day (just INSIDE the envelope) → pre-fix behaviour
    # was already correct and must stay byte-identical: the 1-day window
    # [expiry−30d, full_end) with the STABLE expiry-anchored start.
    win = default_fno_minute_window(
        date(2026, 7, 30),
        full_start=date(2020, 1, 1),
        full_end=date(2026, 7, 1),
        is_option=True,
    )
    assert win == (date(2026, 6, 30), date(2026, 7, 1))


def test_default_window_start_is_stable_across_daily_pulls_once_open() -> None:
    # Once the calendar reaches expiry−lookback the window OPENS with its FIXED
    # start (expiry−30d) — advancing full_end across daily pulls moves ONLY the
    # end, never the start (no moving anchor → no ledger-GC orphan class).
    expiry = date(2026, 7, 31)
    opens_on = date(2026, 7, 1)  # expiry − 30d
    for offset in range(1, 4):  # full_end 2026-07-02 .. 2026-07-04 (window open)
        win = default_fno_minute_window(
            expiry,
            full_start=date(2020, 1, 1),
            full_end=opens_on + timedelta(days=offset),
            is_option=True,
        )
        assert win == (opens_on, opens_on + timedelta(days=offset))


def test_fno_window_opens_on_option_and_future() -> None:
    # The ONE opens_on anchor (COMMON): option = expiry−30d, future = expiry−60d.
    assert fno_window_opens_on(date(2026, 9, 29), is_option=True) == date(2026, 8, 30)
    assert fno_window_opens_on(date(2026, 9, 29), is_option=False) == date(2026, 7, 31)


def test_default_far_expiry_degenerate_envelope_falls_back_to_full() -> None:
    # DEFENSIVE: a degenerate envelope (full_start == full_end) is NOT a
    # not-yet-open skip — it keeps the pre-existing conservative full-range
    # fallback (itself empty) byte-identically, never None and never a crash.
    full = (date(2026, 7, 1), date(2026, 7, 1))
    win = default_fno_minute_window(
        date(2026, 9, 29), full_start=full[0], full_end=full[1], is_option=True
    )
    assert win == full


def test_default_far_expiry_inverted_envelope_falls_back_to_full() -> None:
    # DEFENSIVE (hostile-review NIT): an INVERTED envelope (full_end < full_start)
    # likewise keeps the pre-existing conservative full-range fallback — never
    # None, never a crash, never a widened scope.
    full = (date(2026, 7, 2), date(2026, 7, 1))
    win = default_fno_minute_window(
        date(2026, 9, 29), full_start=full[0], full_end=full[1], is_option=True
    )
    assert win == full


# ─────────────────────────────────────────────────────────────────────────
# contract_daily_coverage (lake read)
# ─────────────────────────────────────────────────────────────────────────
def test_daily_coverage_none_when_no_partitions(tmp_path: Path) -> None:
    # Empty lake → reconcile finds nothing → no partitions → None.
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=OPTION_SEGMENT, interval="1day"
        )
        assert contract_daily_coverage(catalog, contract=_contract()) is None


def test_daily_coverage_spans_min_first_to_max_last(tmp_path: Path) -> None:
    sym = "NSE-NIFTY-30Jan25-23000-CE"
    # Two months → two partitions; coverage = global min(first)..max(last).
    _write_daily(tmp_path, sym, [date(2025, 1, 2), date(2025, 1, 6), date(2025, 2, 3)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=OPTION_SEGMENT, interval="1day"
        )
        cov = contract_daily_coverage(catalog, contract=_contract(sym))
    assert cov == ContractCoverage(first_date=date(2025, 1, 2), last_date=date(2025, 2, 3))


def test_daily_coverage_cross_utc_month_midnight_ist_golden(tmp_path: Path) -> None:
    # GOLDEN (review Finding 4): a midnight-IST 2025-01-01 bar is stored as
    # 2024-12-31T18:30Z (the DECEMBER UTC partition), yet the IST trading day is
    # 2025-01-01. Taking min(first_ts)/max(last_ts) over ALL partitions + IST
    # conversion must recover the IST dates, NOT the UTC dates.
    sym = "NSE-NIFTY-31Jan25-23000-CE"
    _write_daily(tmp_path, sym, [date(2025, 1, 1), date(2025, 2, 1)], clock=time(0, 0))
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=OPTION_SEGMENT, interval="1day"
        )
        cov = contract_daily_coverage(catalog, contract=_contract(sym))
    assert cov == ContractCoverage(first_date=date(2025, 1, 1), last_date=date(2025, 2, 1))


# ─────────────────────────────────────────────────────────────────────────
# derive_contract_minute_windows (driver)
# ─────────────────────────────────────────────────────────────────────────
def test_derive_bounds_omits_no_coverage_and_disjoint(tmp_path: Path) -> None:
    bounded = "NSE-NIFTY-30Jan25-23000-CE"  # 2025 data within range → bounded window
    disjoint = "NSE-NIFTY-15Dec20-23000-CE"  # 2020 data, expiry 2020 → disjoint → omit
    _write_daily(tmp_path, bounded, [date(2025, 1, 2), date(2025, 1, 20)])
    _write_daily(tmp_path, disjoint, [date(2020, 6, 1), date(2020, 6, 10)])
    contracts = [
        _contract(bounded, expiry=date(2025, 1, 30)),
        _contract("NSE-NIFTY-30Jan25-99999-CE", expiry=date(2025, 1, 30)),  # no lake data
        _contract(disjoint, expiry=date(2020, 6, 15)),
    ]
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog, capture_logs() as logs:
        windows = derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=contracts,
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    assert windows == {bounded: (date(2025, 1, 2), date(2025, 1, 31))}
    events = {entry["event"] for entry in logs}
    assert "contract_bounds.disjoint_fallback" in events
    summary = next(e for e in logs if e["event"] == "contract_bounds.derived")
    assert (
        summary["bounded"],
        summary["no_coverage"],
        summary["disjoint"],
        summary["skipped"],
    ) == (
        1,
        1,
        1,
        0,
    )


def test_derive_bounds_reconcile_false_reads_prereconciled_catalog(tmp_path: Path) -> None:
    sym = "NSE-NIFTY-30Jan25-23000-CE"
    _write_daily(tmp_path, sym, [date(2025, 1, 2), date(2025, 1, 20)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        reconcile_catalog_from_lake(
            lake_root=tmp_path, catalog=catalog, segment=OPTION_SEGMENT, interval="1day"
        )
        windows = derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=[_contract(sym, expiry=date(2025, 1, 30))],
            full_start=_FULL_START,
            full_end=_FULL_END,
            reconcile=False,
        )
    assert windows == {sym: (date(2025, 1, 2), date(2025, 1, 31))}


def test_derive_bounds_reconcile_passes_check_invariants_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mid-pull HOT window-derivation reconcile passes ``check_invariants=False``.

    This site never heals — its findings would only re-log what the L2
    post-pull heal handles moments later — so it skips the CPU-only per-file
    ``n_unique`` piggyback (DEC-LAKE-HEAL-001 fix-pass; spy-pinned so the flip
    can never silently regress). The sweep-start / CLI-verify reconcile sites
    deliberately KEEP the default checking.
    """
    import brutex.data.catalog_reconcile as cr

    seen: list[dict[str, Any]] = []
    real = cr.reconcile_catalog_from_lake

    def _spy(**kwargs: Any) -> Any:
        seen.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(cr, "reconcile_catalog_from_lake", _spy)
    sym = "NSE-NIFTY-30Jan25-23000-CE"
    _write_daily(tmp_path, sym, [date(2025, 1, 2)])
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog:
        derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=[_contract(sym, expiry=date(2025, 1, 30))],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    (call,) = seen
    assert call["check_invariants"] is False


def test_derive_bounds_quarantines_malformed_symbol(tmp_path: Path) -> None:
    # A symbol with no exchange token → contract_exchange raises ValueError →
    # that contract is skipped (full-range fallback), the pass continues.
    bad = _contract("NODASH", expiry=date(2025, 1, 30), contract_type="FUT", strike=None)
    with Catalog.open(tmp_path / "catalog.duckdb") as catalog, capture_logs() as logs:
        windows = derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=[bad],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    assert windows == {}
    skipped = next(e for e in logs if e["event"] == "contract_bounds.contract_skipped")
    assert skipped["symbol"] == "NODASH"


def test_derive_bounds_catalog_unavailable_degrades_to_empty(tmp_path: Path) -> None:
    # Files present + a CLOSED catalog → reconcile's upsert raises CatalogError →
    # the whole pass degrades to {} (full range for all) and logs LOUDLY.
    sym = "NSE-NIFTY-30Jan25-23000-CE"
    _write_daily(tmp_path, sym, [date(2025, 1, 2)])
    catalog = Catalog.open(tmp_path / "catalog.duckdb")
    catalog.close()
    with capture_logs() as logs:
        windows = derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=[_contract(sym)],
            full_start=_FULL_START,
            full_end=_FULL_END,
        )
    assert windows == {}
    assert any(e["event"] == "contract_bounds.reconcile_unavailable" for e in logs)


def test_derive_bounds_skips_on_mid_loop_catalog_error(tmp_path: Path) -> None:
    # reconcile=False + a closed catalog → the per-contract list_partitions raises
    # CatalogError, caught per contract (quarantine), pass returns {}.
    catalog = Catalog.open(tmp_path / "catalog.duckdb")
    catalog.close()
    with capture_logs() as logs:
        windows = derive_contract_minute_windows(
            catalog,
            lake_root=tmp_path,
            contracts=[_contract()],
            full_start=_FULL_START,
            full_end=_FULL_END,
            reconcile=False,
        )
    assert windows == {}
    assert any(e["event"] == "contract_bounds.contract_skipped" for e in logs)
