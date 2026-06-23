"""Tab-integrity tests for the ``/movers`` screen — strict spot/futures/options
separation + the bounded 1-minute prior-session fallback.

Covers the two correctness fixes (operator-reported 2026-06-23):

* **Spot is SPOT only** — :func:`~brutex.dashboard.movers_snapshot.compute_spot_candidates`
  must whitelist the cash/index segments (``INDEX``/``CASH``) so an ``FNO``
  contract partition (a future / option) NEVER leaks into the Spot tab.
* **Prior-day baseline from 1-minute** —
  :func:`~brutex.dashboard.movers_snapshot._prior_session_last_from_minute` recovers
  the prior trading day's last close + OI from the stored 1-minute bars (bounded
  back-walk) when the derived ``1day`` partition is absent, so the futures/options
  Price-Gainers/Losers + OI-Gainers/Losers compute on a lake whose 1-day derive
  never ran. The derived 1-day close == the session's last 1-minute close, so the
  recovered value is byte-equal to the 1-day value it substitutes.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from brutex.dashboard.movers_futures import _prev_day_oi
from brutex.dashboard.movers_snapshot import (
    _PREV_SESSION_MAX_BACK_DAYS,
    _prev_day_close,
    _prior_session_last_from_minute,
    compute_spot_candidates,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_IST = ZoneInfo("Asia/Kolkata")


def _min_bar(
    day: date, *, hh: int, mm: int, close: str, oi: int | None = None, volume: int = 10
) -> CandleBar:
    """A 1-minute bar at IST ``hh:mm`` on *day*, stored as a UTC instant."""
    c = Decimal(close)
    ist_dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=_IST)
    return CandleBar(
        timestamp=ist_dt.astimezone(UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(day: date, *, close: str, oi: int | None = None) -> CandleBar:
    """A 1-day bar labelled 09:15 IST on *day* (the derived-1day shape)."""
    c = Decimal(close)
    ist_dt = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
    return CandleBar(
        timestamp=ist_dt.astimezone(UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=1,
        open_interest=oi,
    )


def _write(
    tmp_path: Path, *, segment: str, symbol: str, interval: str, bars: list[CandleBar]
) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=bars
    )


_FUT = "NSE-NIFTY-30Sep25-FUT"


# ── _prior_session_last_from_minute ────────────────────────────────────────


class TestPriorSessionFromMinute:
    def test_no_minute_dir_is_none(self, tmp_path: Path) -> None:
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )

    def test_returns_prior_day_last_close_and_oi(self, tmp_path: Path) -> None:
        # 2025-08-29 (Fri) has two bars; the LAST (15:29 IST) carries the close+OI.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[
                _min_bar(date(2025, 8, 29), hh=9, mm=15, close="100.00", oi=500),
                _min_bar(date(2025, 8, 29), hh=15, mm=29, close="101.50", oi=1234),
            ],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == (Decimal("101.50"), 1234)

    def test_skips_empty_days_back_to_prior_trading_day(self, tmp_path: Path) -> None:
        # day=Mon 2025-09-01 → walks back over Sun 08-31 + Sat 08-30 (no data) to Fri 08-29.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="99.95", oi=7)],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == (Decimal("99.95"), 7)

    def test_null_oi_returns_close_with_none_oi(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="55.25", oi=None)],
        )
        got = _prior_session_last_from_minute(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == (Decimal("55.25"), None)

    def test_beyond_back_bound_is_none(self, tmp_path: Path) -> None:
        # Only data far past the bound → not found within _PREV_SESSION_MAX_BACK_DAYS.
        far = date(2025, 9, 1) - timedelta(days=_PREV_SESSION_MAX_BACK_DAYS + 3)
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(far, hh=15, mm=29, close="10.00", oi=1)],
        )
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )

    def test_only_same_day_data_is_none(self, tmp_path: Path) -> None:
        # A bar ON `day` is not a PRIOR session → None.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 9, 1), hh=15, mm=29, close="42.00", oi=3)],
        )
        assert (
            _prior_session_last_from_minute(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )


# ── _prev_day_close: 1day primary, 1-minute fallback ───────────────────────


class TestPrevDayCloseFallback:
    def test_uses_1day_when_present(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="24000.00", oi=None)],
        )
        # A DIFFERENT 1-minute value must NOT be consulted while 1day has a prior bar.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="99999.00")],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", day=date(2025, 9, 1)
        )
        assert got == Decimal("24000.00")

    def test_falls_back_to_minute_when_no_1day(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="250.75", oi=11)],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == Decimal("250.75")

    def test_falls_back_when_1day_present_but_no_prior_bar(self, tmp_path: Path) -> None:
        # 1day exists only for the SAME day (no prior) → fall back to 1-minute prior.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 9, 1), close="300.00", oi=None)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="298.40", oi=5)],
        )
        got = _prev_day_close(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == Decimal("298.40")

    def test_none_when_neither_source(self, tmp_path: Path) -> None:
        assert (
            _prev_day_close(
                tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
            )
            is None
        )


# ── futures _prev_day_oi: 1-minute fallback ────────────────────────────────


class TestPrevDayOiFallback:
    def test_falls_back_to_minute_oi_when_no_1day(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=4321)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 4321

    def test_1day_oi_takes_precedence(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="100.00", oi=999)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=4321)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 999

    def test_1day_present_but_null_oi_falls_back_to_minute(self, tmp_path: Path) -> None:
        # The universal real-world F&O case: a derived 1-day bar EXISTS (with a real
        # close) but its open_interest is null (the derive writes OI=None), so the
        # OI must be recovered from the prior session's last 1-minute bar.
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1day",
            bars=[_day_bar(date(2025, 8, 29), close="100.00", oi=None)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(date(2025, 8, 29), hh=15, mm=29, close="100.00", oi=8765)],
        )
        got = _prev_day_oi(
            tmp_path, exchange="NSE", segment="FNO", symbol=_FUT, day=date(2025, 9, 1)
        )
        assert got == 8765


# ── compute_spot_candidates: SPOT-only segment whitelist ───────────────────


class TestSpotSegmentWhitelist:
    def test_fno_contract_excluded_from_spot(self, tmp_path: Path) -> None:
        snap_day = date(2025, 9, 1)
        # A real spot index (INDEX) + an F&O future (FNO) — both with 1-minute on the day.
        _write(
            tmp_path,
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_min_bar(snap_day, hh=9, mm=15, close="24000.00", volume=100)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=_FUT,
            interval="1minute",
            bars=[_min_bar(snap_day, hh=9, mm=15, close="24050.00", volume=100)],
        )
        candidates = compute_spot_candidates(
            lake_root=tmp_path, interval="1minute", date=snap_day.isoformat(), time=None
        )
        symbols = {s.instrument.symbol for s in candidates.snapshots}
        assert "NIFTY" in symbols
        assert _FUT not in symbols  # the future must NOT pollute the Spot tab
        assert all(s.instrument.segment in {"INDEX", "CASH"} for s in candidates.snapshots)
