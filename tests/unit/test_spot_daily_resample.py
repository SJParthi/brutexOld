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
from pathlib import Path

import polars as pl
import pytest
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar
from brutex.data.spot_daily_resample import (
    DailyDeriveReport,
    DailyDeriveTarget,
    DailyFillResult,
    DailyReconcileReport,
    DailyResampleWindow,
    derive_daily_for_targets,
    fill_daily_from_minute,
    frame_to_candle_bars,
    ist_span_partition_files,
    ist_span_partition_months,
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
    # ... so its retry split is a KNOWN zero on both halves, never a fabricated
    # unknown (DEC-RETRY-COUNTER-SPLIT-001 — mirrors the in_pull_fetches=0 fact).
    assert filled.empty_retries == 0
    assert filled.transient_retries == 0


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


def test_derive_targets_lakewritehalt_propagates_not_swallowed() -> None:
    """A full-disk / OOM at the 1-day write boundary HALTS the pull, never a
    false-success `errored` count.

    ``LakeWriteHalt`` is the pull-WIDE global stop (disk-full / OOM); the bare
    per-target ``except Exception`` must re-raise it (mirroring the sibling
    escape guards in ``lake_heal._rebuild_tf_months`` +
    ``crossverify.prebuild_leg``) instead of quarantining it as a per-target
    DATA error and continuing — otherwise a full disk reads as an ok pass.
    """

    def _write_halts(_bars: Sequence[CandleBar]) -> int:
        raise LakeWriteHalt("disk_full")

    with pytest.raises(LakeWriteHalt):
        derive_daily_for_targets(
            # A non-empty slice so the resampler yields a bar and the writer
            # (which halts) is actually invoked.
            targets=[_target("NSE-NIFTY")],
            build_read_1m=lambda _t: lambda: _frame((date(2025, 11, 28), 14000.0)),
            build_write_1day=lambda _t: _write_halts,
            before=_BEFORE,
        )


def test_derive_targets_lakewritehalt_aborts_batch_before_later_targets() -> None:
    """The global-stop re-raise aborts the whole batch — a later target is
    never processed once one target hits ``LakeWriteHalt`` — while a genuine
    per-target DATA error (``ValueError``) STILL isolates and counts as
    ``errored`` (per-target isolation of NON-global errors is preserved)."""
    seen: list[str] = []

    def _read(target: DailyDeriveTarget):  # type: ignore[no-untyped-def]
        seen.append(target.symbol)
        return lambda: _frame((date(2025, 11, 28), 14000.0))

    def _write(target: DailyDeriveTarget):  # type: ignore[no-untyped-def]
        if target.symbol == "NSE-NIFTY":

            def _halt(_bars: Sequence[CandleBar]) -> int:
                raise LakeWriteHalt("disk_full")

            return _halt

        def _data_error(_bars: Sequence[CandleBar]) -> int:  # pragma: no cover
            raise ValueError("corrupt slice")

        return _data_error

    with pytest.raises(LakeWriteHalt):
        derive_daily_for_targets(
            targets=[_target("NSE-NIFTY"), _target("NSE-BANKNIFTY")],
            build_read_1m=_read,
            build_write_1day=_write,
            before=_BEFORE,
        )
    # The first target aborted the batch — the second was never reached.
    assert seen == ["NSE-NIFTY"]


def test_derive_targets_data_error_at_write_still_isolates() -> None:
    """A NON-global write error (``ValueError``) STILL counts as ``errored``
    and continues — the fix must not widen the escape past ``LakeWriteHalt``."""

    def _write(_bars: Sequence[CandleBar]) -> int:
        raise ValueError("corrupt slice — not a global stop")

    report = derive_daily_for_targets(
        targets=[_target("NSE-NIFTY")],
        build_read_1m=lambda _t: lambda: _frame((date(2025, 11, 28), 14000.0)),
        build_write_1day=lambda _t: _write,
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


# ── Bounded post-pull read: month-partition selection (DEC-RESAMPLE-BOUND-GLOB-001) ──
#
# The post-pull 1-day derivation no longer reads each symbol's WHOLE 1-minute
# partition tree (O(lake-depth)) — it globs ONLY the <yyyy>/<mm>.parquet months the
# pulled IST span covers (O(pulled-window)). These pin the pure month-set computation
# + the on-disk existing-file selection (single month, multi-month, cross-year, empty,
# absent-month skip), so the integration round-trip can assert STRICTLY-FEWER files.


class TestIstSpanPartitionMonths:
    """Pure UTC ``(year, month)`` selection for an IST ``[start, end)`` span."""

    def test_single_month(self) -> None:
        assert ist_span_partition_months(date(2025, 3, 5), date(2025, 3, 20)) == [(2025, 3)]

    def test_multi_month_same_year(self) -> None:
        assert ist_span_partition_months(date(2025, 3, 5), date(2025, 5, 2)) == [
            (2025, 3),
            (2025, 4),
            (2025, 5),
        ]

    def test_cross_year_boundary(self) -> None:
        # Dec 2025 → Jan 2026; the Dec→Jan month rollover branch.
        assert ist_span_partition_months(date(2025, 12, 20), date(2026, 1, 10)) == [
            (2025, 12),
            (2026, 1),
        ]

    def test_empty_span_is_no_op(self) -> None:
        assert ist_span_partition_months(date(2025, 3, 5), date(2025, 3, 5)) == []

    def test_inverted_span_is_no_op(self) -> None:
        assert ist_span_partition_months(date(2025, 3, 10), date(2025, 3, 5)) == []

    def test_first_of_month_pads_prior_month(self) -> None:
        # ``start`` on the 1st → the −1-day pad reaches the PRIOR month, so a bar that
        # somehow landed in the prior UTC partition is never globbed out. ``end`` on a
        # month boundary likewise includes that boundary month via the +1-day pad.
        assert ist_span_partition_months(date(2025, 3, 1), date(2025, 4, 1)) == [
            (2025, 2),
            (2025, 3),
            (2025, 4),
        ]

    def test_jan_start_pads_to_prior_december(self) -> None:
        # Pad across a year boundary on the LOW side: Jan 1 − 1 day = Dec 31 (prior yr).
        assert ist_span_partition_months(date(2026, 1, 1), date(2026, 1, 15)) == [
            (2025, 12),
            (2026, 1),
        ]


def _touch_month(base: Path, year: int, month: int) -> Path:
    """Create an empty ``<base>/<yyyy>/<mm>.parquet`` file and return its path."""
    year_dir = base / f"{year:04d}"
    year_dir.mkdir(parents=True, exist_ok=True)
    f = year_dir / f"{month:02d}.parquet"
    f.write_bytes(b"")
    return f


class TestIstSpanPartitionFiles:
    """On-disk existing ``<yyyy>/<mm>.parquet`` selection for an IST span."""

    def test_returns_only_existing_covered_months(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        f_mar = _touch_month(base, 2025, 3)
        # April deliberately ABSENT — must be skipped cleanly, not error.
        f_may = _touch_month(base, 2025, 5)
        _touch_month(base, 2023, 1)  # far-outside the span → never globbed
        # Covered span [Mar 5, May 15) → months {Feb(pad), Mar, Apr, May}; on disk only
        # Mar + May exist within that set (Feb + Apr absent → skipped cleanly), and the
        # out-of-span 2023 file is NOT read — proof the read is bounded, not whole-tree.
        files = ist_span_partition_files(base, date(2025, 3, 5), date(2025, 5, 15))
        assert files == [f_mar.as_posix(), f_may.as_posix()]

    def test_empty_span_reads_nothing(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _touch_month(base, 2025, 3)
        assert ist_span_partition_files(base, date(2025, 3, 5), date(2025, 3, 5)) == []

    def test_no_covered_month_on_disk(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        _touch_month(base, 2024, 6)  # exists, but OUTSIDE the queried span
        assert ist_span_partition_files(base, date(2030, 1, 1), date(2030, 2, 1)) == []

    def test_cross_year_existing_files(self, tmp_path: Path) -> None:
        base = tmp_path / "1minute"
        f_dec = _touch_month(base, 2025, 12)
        f_jan = _touch_month(base, 2026, 1)
        files = ist_span_partition_files(base, date(2025, 12, 20), date(2026, 1, 10))
        assert files == [f_dec.as_posix(), f_jan.as_posix()]

    def test_missing_base_dir_is_empty(self, tmp_path: Path) -> None:
        # No ``1minute`` tree at all → honest empty (a clean no-op, never an error).
        base = tmp_path / "1minute"
        assert ist_span_partition_files(base, date(2025, 3, 1), date(2025, 4, 1)) == []


# ── the prebuild live-edge divergence, PINNED (DEC-DOCTOR-KIT-HONESTY-001, fix 4) ──


class TestPrebuildLiveEdgeDivergencePinned:
    """The operator's 122-vs-123 prebuild gap is DELIBERATE, not an off-by-one.

    Forensics (log 23c543ba): the prebuild wrote 122 ADANIENT ``1day`` bars
    while every one of the 20 intraday TFs derived 123 sessions from the SAME
    1-minute data. Root (span arithmetic, pinned here): the daily derive runs
    with ``before = run-day IST`` (``materialize_lake_timeframes`` →
    ``fill_daily_from_minute``) and :func:`resample_1m_to_daily` DROPS any
    session whose IST date is ``>= before`` — the no-look-ahead
    forming-session guard — while the intraday materialisation
    (:func:`brutex.data.tf_resampler.resample_1m_to_tf`) has no such guard.
    So when the stored 1-minute contains the run day's own session, EXACTLY
    ONE session (the live edge) is absent from the ``1day`` partition —
    since DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001 for a MID-SESSION run ONLY:
    the production default ``before`` is now the session-close-aware
    :func:`brutex.calendars.nse.daily_derive_cutoff`, so a POST-CLOSE run
    passes ``before = run_day + 1`` and derives the run day too (superseding
    the fix-4 whole-day-granular clause). The PRIMITIVE semantics pinned here
    are UNCHANGED — ``resample_1m_to_daily`` drops ``ist_date >= before``
    verbatim for whatever ``before`` the caller resolves — and a mid-session
    run still SELF-HEALS on the next pipeline run (latest-wins derived
    ``1day``). The 2025/12-partition observation is the midnight-IST
    stamping, also deliberate: an IST date-D daily bar stamps (D−1) 18:30
    UTC, so the IST 2026-01-01 session's bar lives in the DECEMBER UTC month
    partition. These tests pin the primitive's documented, intended
    semantics.
    """

    def test_daily_excludes_exactly_the_run_day_session_intraday_keeps_it(self) -> None:
        from brutex.data.tf_resampler import resample_1m_to_tf

        frame = _frame(
            (date(2026, 7, 1), 100.0),
            (date(2026, 7, 2), 110.0),
            (date(2026, 7, 3), 120.0),  # the run day's own (live-edge) session
        )
        run_day = date(2026, 7, 3)
        daily = resample_1m_to_daily(frame, before=run_day)
        daily_days = [bar.timestamp.astimezone(_IST).date() for bar in daily]
        assert daily_days == [date(2026, 7, 1), date(2026, 7, 2)]
        intraday = resample_1m_to_tf(frame, 60)
        intraday_days = sorted(
            {(ts + timedelta(hours=5, minutes=30)).date() for ts in intraday["ts_utc"].to_list()}
        )
        assert intraday_days == [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        # The pinned divergence: EXACTLY one session — the live edge — differs.
        assert len(intraday_days) - len(daily_days) == 1

    def test_daily_bar_for_first_ist_session_of_month_lands_in_prior_utc_month(self) -> None:
        # The refuter's 2025/12 observation: the IST 2026-01-01 session's daily
        # bar stamps 2025-12-31T18:30Z (midnight-IST labeling) → the DECEMBER
        # UTC month partition. Shifted, never missing.
        frame = _frame((date(2026, 1, 1), 100.0))
        (bar,) = resample_1m_to_daily(frame, before=date(2026, 1, 2))
        assert bar.timestamp == datetime(2025, 12, 31, 18, 30, tzinfo=UTC)
        assert (bar.timestamp.year, bar.timestamp.month) == (2025, 12)
        assert bar.timestamp.astimezone(_IST).date() == date(2026, 1, 1)

    def test_post_close_run_day_still_excluded_and_next_day_run_writes_it(self) -> None:
        # PRIMITIVE pin: with before = D the run day's session is excluded and
        # with before = D+1 it derives — exactly the two cutoffs
        # daily_derive_cutoff resolves for a mid-session vs a post-close /
        # non-trading-day run (DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001). A
        # post-close PRODUCTION run now derives D directly; a mid-session run
        # still self-heals on the next run (latest-wins derived 1day).
        frame = _frame((date(2026, 7, 3), 120.0))
        assert resample_1m_to_daily(frame, before=date(2026, 7, 3)) == []
        bars = resample_1m_to_daily(frame, before=date(2026, 7, 4))
        assert [bar.timestamp.astimezone(_IST).date() for bar in bars] == [date(2026, 7, 3)]


class TestResampleVectorizedParity:
    """Vectorized ``resample_1m_to_daily`` == the verbatim legacy per-row loop.

    DEC-PREBUILD-GIL-VECTORIZE-001 replaced the per-row ``iter_rows`` →
    ``astimezone(_IST)`` → ``CandleBar(Decimal(str(...)) × 4)`` loop with a
    columnar filter/re-stamp + the batched
    :func:`brutex.data.spot_daily_resample.frame_to_candle_bars` (which lives
    here — the import-graph leaf — and is re-exported from
    ``timeframe_materialize``). The oracle
    below is the pre-change implementation copied byte-for-byte; every bar —
    instant, prices, volume, OI — must match over the edge shapes (IST date
    roll past 18:30 UTC, month/year boundary, forming-session cutoff,
    float-noise prices).
    """

    @staticmethod
    def _legacy_resample_1m_to_daily(df_1m: pl.DataFrame, *, before: date) -> list[CandleBar]:
        from decimal import Decimal

        from brutex.data.tf_resampler import resample_1m_to_tf
        from brutex.data.timeframes import DAILY_TF_MINUTES

        if df_1m.is_empty():
            return []
        daily = resample_1m_to_tf(df_1m, DAILY_TF_MINUTES)
        bars: list[CandleBar] = []
        for row in daily.iter_rows(named=True):
            ist_date = row["ts_utc"].astimezone(_IST).date()
            if ist_date >= before:
                continue
            stamp_utc = datetime(
                ist_date.year, ist_date.month, ist_date.day, tzinfo=_IST
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=stamp_utc,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row["volume"]),
                    open_interest=None,
                )
            )
        return bars

    def _assert_parity(self, frame: pl.DataFrame, *, before: date) -> list[CandleBar]:
        new = resample_1m_to_daily(frame, before=before)
        legacy = self._legacy_resample_1m_to_daily(frame, before=before)
        assert new == legacy
        # The UTC instant AND its rendering are identical (tzinfo object type may
        # differ — ZoneInfo("UTC") vs timezone.utc — but the instant, offset and
        # isoformat are what the lake/dedup identity keys on).
        for new_bar, legacy_bar in zip(new, legacy, strict=True):
            assert new_bar.timestamp.utcoffset() == legacy_bar.timestamp.utcoffset()
            assert new_bar.timestamp.isoformat() == legacy_bar.timestamp.isoformat()
        return new

    def test_multi_session_month_and_year_boundary(self) -> None:
        bars = self._assert_parity(
            _frame(
                (date(2025, 11, 28), 14000.0),
                (date(2025, 12, 31), 14100.0),
                (date(2026, 1, 1), 14200.0),
            ),
            before=_BEFORE,
        )
        assert len(bars) == 3
        assert all(bar.open_interest is None for bar in bars)

    def test_post_1830_utc_minute_rolls_ist_date(self) -> None:
        # A bar at 18:31 UTC belongs to the NEXT IST calendar date (00:01 IST) —
        # the columnar +05:30 shift must classify it exactly like astimezone(_IST).
        rows = [
            *_session_rows(date(2025, 11, 28), 14000.0),
            {
                "ts_utc": datetime(2025, 11, 28, 18, 31, tzinfo=UTC),
                "open": 14020.7999,
                "high": 14022.15,
                "low": 14019.05,
                "close": 14021.95,
                "volume": 5,
            },
        ]
        frame = pl.DataFrame(rows, schema=_1M_SCHEMA).sort("ts_utc")
        bars = self._assert_parity(frame, before=_BEFORE)
        # Two IST sessions: 28-Nov (the in-session bars) + 29-Nov (the 00:01 IST bar).
        assert [bar.timestamp.astimezone(_IST).date() for bar in bars] == [
            date(2025, 11, 28),
            date(2025, 11, 29),
        ]

    def test_forming_session_cutoff_parity(self) -> None:
        frame = _frame((date(2025, 11, 28), 14000.0), (date(2025, 12, 1), 14100.0))
        bars = self._assert_parity(frame, before=date(2025, 12, 1))
        assert [bar.timestamp.astimezone(_IST).date() for bar in bars] == [date(2025, 11, 28)]

    def test_empty_frame_parity(self) -> None:
        assert self._assert_parity(pl.DataFrame(schema=_1M_SCHEMA), before=_BEFORE) == []

    def test_float_noise_prices_parity(self) -> None:
        # Long-tail shortest-repr floats (the DEC-LAKE-WRITE-VECTORIZE-001 boundary
        # class) — Decimal(polars cast-String) must equal Decimal(str(x)) per value.
        rows = [
            {
                "ts_utc": datetime(2025, 11, 28, 3, 45 + i, tzinfo=UTC),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 10 + i,
            }
            for i, (o, h, low, c) in enumerate(
                [
                    (14020.7999, 14022.15, 14019.0001, 14021.95),
                    (0.00499999999, 1.0, 0.00499999999, 0.005),
                    (123456.789012345, 123457.0, 123456.0, 123456.789012345),
                ]
            )
        ]
        frame = pl.DataFrame(rows, schema=_1M_SCHEMA).sort("ts_utc")
        self._assert_parity(frame, before=_BEFORE)


class TestFrameToCandleBarsOpenInterest:
    """The ``open_interest``-column arm of :func:`frame_to_candle_bars`.

    The batched frame → ``CandleBar`` helper LIVES here (the import-graph
    leaf, re-exported from ``timeframe_materialize`` for its original
    callers), so its OI-column branch must be covered by THIS module's own
    per-module 100% gate — not only by the timeframe_materialize suite that
    exercises it through the intraday-TF writers. Contract (the legacy
    ``_oi`` helper's): a present-but-null OI maps to *absent_open_interest*;
    a present int passes through.
    """

    @staticmethod
    def _frame_with_oi(open_interests: list[int | None]) -> pl.DataFrame:
        rows = [
            {
                "ts_utc": datetime(2026, 6, 18, 3, 45 + i, tzinfo=UTC),
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 99.0 + i,
                "close": 101.0 + i,
                "volume": 10 + i,
                "open_interest": oi,
            }
            for i, oi in enumerate(open_interests)
        ]
        return pl.DataFrame(rows, schema={**_1M_SCHEMA, "open_interest": pl.Int64})

    def test_present_oi_column_int_passes_through_and_null_maps_to_default(self) -> None:
        bars = frame_to_candle_bars(self._frame_with_oi([None, 75]))
        assert [bar.open_interest for bar in bars] == [0, 75]
        # The rest of the bar is the same batched conversion the no-OI path uses.
        assert bars[0].timestamp == datetime(2026, 6, 18, 3, 45, tzinfo=UTC)
        assert str(bars[0].open) == "100.0"
        assert [bar.volume for bar in bars] == [10, 11]

    def test_present_null_oi_respects_explicit_absent_value(self) -> None:
        bars = frame_to_candle_bars(self._frame_with_oi([None, 42]), absent_open_interest=None)
        assert [bar.open_interest for bar in bars] == [None, 42]
