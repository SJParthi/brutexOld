"""100% line+branch tests for :mod:`brutex.data.spot_daily_resample`.

Covers the pure resample-on-empty core (DEC-SPOT-1DAY-RESAMPLE-ON-EMPTY-001):
the midnight-IST re-stamp (the linchpin), the OHLCV aggregation reuse, the
forming-session guard, the empty/no-minute no-op, the ledger candidate planner,
and the quarantining reconcile driver (filled / no-minute / errored arms). The raw
lake builders (``make_lake_read_1m_window`` / ``make_lake_write_1day``) are
``# pragma: no cover`` — exercised by the round-trip integration test, not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone

import polars as pl
import pytest
from brutex.data.models import CandleBar
from brutex.data.spot_daily_resample import (
    DailyDeriveReport,
    DailyDeriveTarget,
    DailyFillResult,
    DailyReconcileReport,
    DailyResampleWindow,
    derive_daily_for_targets,
    fill_daily_from_minute,
    plan_daily_resample_windows,
    reconcile_daily_from_minute,
    resample_1m_to_daily,
)
from brutex.data.window_ledger import (
    EMPTY,
    FAILED,
    PLANNED,
    SKIPPED,
    SUCCESS,
    WindowLedger,
    WindowLedgerRecord,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_NOW = datetime(2026, 6, 19, 6, 0, tzinfo=UTC)
_BEFORE = date(2026, 6, 19)

_1M_SCHEMA = {
    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
}


def _session_rows(day: date, base: float) -> list[dict[str, object]]:
    """Three 1-minute bars across one IST session (09:15 → 15:30 IST)."""
    spec = [
        (3, 45, base, base + 5.0, base - 2.0, base + 1.0, 100),  # 09:15 IST (open)
        (6, 0, base + 1.0, base + 8.0, base - 1.0, base + 4.0, 200),  # mid-session
        (10, 0, base + 4.0, base + 6.0, base + 2.0, base + 3.0, 150),  # ~15:30 IST (close)
    ]
    return [
        {
            "ts_utc": datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC),
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": v,
        }
        for (hh, mm, o, h, low, c, v) in spec
    ]


def _frame(*days_bases: tuple[date, float]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for day, base in days_bases:
        rows.extend(_session_rows(day, base))
    return pl.DataFrame(rows, schema=_1M_SCHEMA).sort("ts_utc")


# ── resample_1m_to_daily ─────────────────────────────────────────────────────


def test_resample_empty_frame_returns_empty_list() -> None:
    assert resample_1m_to_daily(pl.DataFrame(schema=_1M_SCHEMA), before=_BEFORE) == []


def test_resample_midnight_ist_stamp_and_ohlcv() -> None:
    # 2025-11-28 (Fri) IST session — the vendor-quirk window's shape.
    bars = resample_1m_to_daily(_frame((date(2025, 11, 28), 14000.0)), before=_BEFORE)
    assert len(bars) == 1
    bar = bars[0]
    # The linchpin: midnight IST == 18:30 UTC of the PRIOR calendar day.
    assert bar.timestamp == datetime(2025, 11, 27, 18, 30, tzinfo=UTC)
    assert bar.timestamp.astimezone(_IST) == datetime(2025, 11, 28, 0, 0, tzinfo=_IST)
    # OHLCV: open=first, high=max, low=min, close=last, volume=sum.
    assert (bar.open, bar.high, bar.low, bar.close) == (14000.0, 14008.0, 13998.0, 14003.0)
    assert bar.volume == 450
    assert bar.open_interest is None  # spot/index daily carries no OI


def test_resample_multi_session_ascending() -> None:
    bars = resample_1m_to_daily(
        _frame((date(2025, 11, 28), 14000.0), (date(2025, 12, 1), 14100.0)), before=_BEFORE
    )
    assert [b.timestamp for b in bars] == [
        datetime(2025, 11, 27, 18, 30, tzinfo=UTC),
        datetime(2025, 11, 30, 18, 30, tzinfo=UTC),
    ]


def test_resample_forming_session_dropped() -> None:
    # before == the second session's IST date → that session is incomplete → dropped;
    # the earlier complete session is kept (both branches of the guard exercised).
    bars = resample_1m_to_daily(
        _frame((date(2025, 11, 28), 14000.0), (date(2025, 12, 1), 14100.0)),
        before=date(2025, 12, 1),
    )
    assert [b.timestamp.astimezone(_IST).date() for b in bars] == [date(2025, 11, 28)]


def test_resample_price_noise_kept_full_precision() -> None:
    # Float read-back noise is preserved as Decimal here (the sink snaps on write).
    rows = [
        {
            "ts_utc": datetime(2025, 11, 28, 3, 45, tzinfo=UTC),
            "open": 14020.7999999,
            "high": 14020.7999999,
            "low": 14020.7999999,
            "close": 14020.7999999,
            "volume": 5,
        }
    ]
    bar = resample_1m_to_daily(pl.DataFrame(rows, schema=_1M_SCHEMA), before=_BEFORE)[0]
    assert str(bar.open) == "14020.7999999"


# ── fill_daily_from_minute ───────────────────────────────────────────────────


def test_fill_no_minute_is_noop() -> None:
    calls: list[Sequence[CandleBar]] = []

    def _write(bars: Sequence[CandleBar]) -> int:
        calls.append(bars)
        return len(bars)

    result = fill_daily_from_minute(
        read_1m=lambda: pl.DataFrame(schema=_1M_SCHEMA), write_1day=_write, before=_BEFORE
    )
    assert result == DailyFillResult(bars_written=0, first_ts=None, last_ts=None)
    assert calls == []  # write is NEVER called on an empty slice (no invented bar)


def test_fill_writes_and_reports_span() -> None:
    written: list[int] = []

    def _write(bars: Sequence[CandleBar]) -> int:
        written.append(len(bars))
        return len(bars)

    result = fill_daily_from_minute(
        read_1m=lambda: _frame((date(2025, 11, 28), 14000.0), (date(2025, 12, 1), 14100.0)),
        write_1day=_write,
        before=_BEFORE,
    )
    assert written == [2]
    assert result.bars_written == 2
    assert result.first_ts == datetime(2025, 11, 27, 18, 30, tzinfo=UTC)
    assert result.last_ts == datetime(2025, 11, 30, 18, 30, tzinfo=UTC)


# ── plan_daily_resample_windows ──────────────────────────────────────────────


def _rec(
    *,
    symbol: str = "RELIANCE",
    interval: str = "1day",
    status: str = EMPTY,
    empty_streak: int = 1,
    start: str = "2025-11-30",
    end: str = "2026-05-29",
) -> WindowLedgerRecord:
    return WindowLedgerRecord(
        market="GROWW",
        security_id=symbol,
        interval=interval,
        window_start=start,
        window_end=end,
        status=status,
        bars=0,
        attempts=1,
        last_error=None,
        first_seen_at=_NOW.isoformat(),
        last_attempt_at=_NOW.isoformat(),
        empty_streak=empty_streak,
    )


def test_plan_selects_empty_and_skipped_via_empty() -> None:
    plan = plan_daily_resample_windows(
        [
            _rec(status=EMPTY, empty_streak=1),
            _rec(symbol="TCS", status=SKIPPED, empty_streak=3),
        ]
    )
    assert {w.security_id for w in plan} == {"RELIANCE", "TCS"}
    w = plan[0]
    assert (w.market, w.interval, w.start, w.end) == (
        "GROWW",
        "1day",
        date(2025, 11, 30),
        date(2026, 5, 29),
    )


@pytest.mark.parametrize(
    "record",
    [
        _rec(interval="1minute"),  # not the daily interval
        _rec(status=SUCCESS),  # has data
        _rec(status=FAILED),  # transient, not empty-settled
        _rec(status=PLANNED),  # never fetched
        _rec(status=SKIPPED, empty_streak=0),  # definitive known-gap, not empty-derived
    ],
)
def test_plan_excludes_non_candidates(record: WindowLedgerRecord) -> None:
    assert plan_daily_resample_windows([record]) == []


def test_plan_parses_full_iso_window_bound() -> None:
    plan = plan_daily_resample_windows([_rec(start="2025-11-30T00:00:00", end="2026-05-29")])
    assert plan[0].start == date(2025, 11, 30)


def test_plan_custom_daily_interval() -> None:
    assert plan_daily_resample_windows([_rec(interval="D")], daily_interval="D")[0].interval == "D"


# ── reconcile_daily_from_minute ──────────────────────────────────────────────


def _seed_empty(ledger: WindowLedger, window: DailyResampleWindow) -> None:
    """Seed *ledger* with an EMPTY daily record for *window* (the pre-fill state)."""
    from brutex.data.fetch_coverage import WindowKey

    ledger.record(
        WindowKey(
            market=window.market,
            security_id=window.security_id,
            interval=window.interval,
            start=window.start,
            end=window.end,
        ),
        raw_bars=0,
        error_type=None,
        bars_appended=0,
        now=_NOW,
    )


def test_reconcile_fills_records_success_with_zero_fetches() -> None:
    ledger = WindowLedger()
    window = DailyResampleWindow("GROWW", "RELIANCE", "1day", date(2025, 11, 28), date(2025, 12, 2))
    _seed_empty(ledger, window)
    assert ledger.records()[0].status == EMPTY
    assert ledger.records()[0].empty_streak == 1

    report = reconcile_daily_from_minute(
        records=ledger.records(),
        ledger=ledger,
        build_read_1m=lambda _w: lambda: _frame((date(2025, 11, 28), 14000.0)),
        build_write_1day=lambda _w: len,
        before=_BEFORE,
        now=_NOW,
    )
    assert report == DailyReconcileReport(filled=1, no_minute=0, errored=0, bars=1)
    assert report.windows == 1
    filled = ledger.records()[0]
    assert filled.status == SUCCESS
    assert filled.bars == 1
    assert filled.empty_streak == 0  # SUCCESS resets the streak — never re-promoted
    assert filled.in_pull_fetches == 0  # a resample made ZERO vendor calls


def test_reconcile_no_minute_leaves_window_empty() -> None:
    ledger = WindowLedger()
    window = DailyResampleWindow("GROWW", "TCS", "1day", date(2025, 11, 28), date(2025, 12, 2))
    _seed_empty(ledger, window)

    report = reconcile_daily_from_minute(
        records=ledger.records(),
        ledger=ledger,
        build_read_1m=lambda _w: lambda: pl.DataFrame(schema=_1M_SCHEMA),
        build_write_1day=lambda _w: len,
        before=_BEFORE,
        now=_NOW,
    )
    assert report == DailyReconcileReport(filled=0, no_minute=1, errored=0, bars=0)
    assert ledger.records()[0].status == EMPTY  # untouched — no invented data


def test_reconcile_quarantines_per_window_error() -> None:
    ledger = WindowLedger()
    window = DailyResampleWindow("GROWW", "BADSYM", "1day", date(2025, 11, 28), date(2025, 12, 2))
    _seed_empty(ledger, window)

    def _boom(_w: DailyResampleWindow):  # type: ignore[no-untyped-def]
        raise KeyError("symbol not in by_symbol")

    report = reconcile_daily_from_minute(
        records=ledger.records(),
        ledger=ledger,
        build_read_1m=_boom,
        build_write_1day=lambda _w: len,
        before=_BEFORE,
        now=_NOW,
    )
    assert report == DailyReconcileReport(filled=0, no_minute=0, errored=1, bars=0)
    assert ledger.records()[0].status == EMPTY  # the bad window never corrupts the ledger


def test_reconcile_mixed_batch() -> None:
    ledger = WindowLedger()
    fillable = DailyResampleWindow(
        "GROWW", "RELIANCE", "1day", date(2025, 11, 28), date(2025, 12, 2)
    )
    empty_sym = DailyResampleWindow("GROWW", "TCS", "1day", date(2025, 11, 28), date(2025, 12, 2))
    _seed_empty(ledger, fillable)
    _seed_empty(ledger, empty_sym)

    def _read(window: DailyResampleWindow):  # type: ignore[no-untyped-def]
        if window.security_id == "RELIANCE":
            return lambda: _frame((date(2025, 11, 28), 14000.0))
        return lambda: pl.DataFrame(schema=_1M_SCHEMA)

    report = reconcile_daily_from_minute(
        records=ledger.records(),
        ledger=ledger,
        build_read_1m=_read,
        build_write_1day=lambda _w: len,
        before=_BEFORE,
        now=_NOW,
    )
    assert report == DailyReconcileReport(filled=1, no_minute=1, errored=0, bars=1)


# ── derive_daily_for_targets (always-on — DEC-1MINUTE-ONLY-DERIVE-1DAY-001) ──


def _target(symbol: str = "NSE-NIFTY") -> DailyDeriveTarget:
    return DailyDeriveTarget(
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        start=date(2025, 11, 1),
        end=date(2025, 12, 31),
    )


def test_derive_targets_report_property() -> None:
    rep = DailyDeriveReport(derived=2, no_minute=1, errored=1, bars=5)
    assert rep.targets == 4


def test_derive_targets_derives_and_tallies() -> None:
    written: list[int] = []

    def _write(bars: Sequence[CandleBar]) -> int:
        written.append(len(bars))
        return len(bars)

    report = derive_daily_for_targets(
        targets=[_target("NSE-NIFTY")],
        build_read_1m=lambda _t: (
            lambda: _frame((date(2025, 11, 28), 14000.0), (date(2025, 12, 1), 14100.0))
        ),
        build_write_1day=lambda _t: _write,
        before=_BEFORE,
    )
    assert report == DailyDeriveReport(derived=1, no_minute=0, errored=0, bars=2)
    assert report.targets == 1
    assert written == [2]


def test_derive_targets_no_minute_is_noop() -> None:
    calls: list[object] = []

    def _write(bars: Sequence[CandleBar]) -> int:
        calls.append(bars)
        return len(bars)

    report = derive_daily_for_targets(
        targets=[_target("NSE-NIFTY")],
        build_read_1m=lambda _t: lambda: pl.DataFrame(schema=_1M_SCHEMA),
        build_write_1day=lambda _t: _write,
        before=_BEFORE,
    )
    assert report == DailyDeriveReport(derived=0, no_minute=1, errored=0, bars=0)
    assert calls == []  # write NEVER called on an empty slice (no invented bar)


def test_derive_targets_quarantines_per_target_error() -> None:
    def _boom(_t: DailyDeriveTarget):  # type: ignore[no-untyped-def]
        raise KeyError("symbol not in by_symbol")

    report = derive_daily_for_targets(
        targets=[_target("BADSYM")],
        build_read_1m=_boom,
        build_write_1day=lambda _t: len,
        before=_BEFORE,
    )
    assert report == DailyDeriveReport(derived=0, no_minute=0, errored=1, bars=0)


def test_derive_targets_mixed_batch() -> None:
    def _read(target: DailyDeriveTarget):  # type: ignore[no-untyped-def]
        if target.symbol == "NSE-NIFTY":
            return lambda: _frame((date(2025, 11, 28), 14000.0))
        return lambda: pl.DataFrame(schema=_1M_SCHEMA)

    report = derive_daily_for_targets(
        targets=[_target("NSE-NIFTY"), _target("NSE-BANKNIFTY")],
        build_read_1m=_read,
        build_write_1day=lambda _t: len,
        before=_BEFORE,
    )
    assert report == DailyDeriveReport(derived=1, no_minute=1, errored=0, bars=1)
