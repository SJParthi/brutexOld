"""Unit tests for ``brutex.data.fno_daily_resample`` (DEC-FNO-TF-DERIVE-OI-001).

Covers the F&O daily derive (carry-last-of-day OI + midnight-IST stamp + the
forming-session guard) and its edges:
* OI = last-of-day (not sum / first / max).
* midnight-IST re-stamp (byte-identical to a vendor ``00:00:00 IST`` daily bar).
* sparse/illiquid gap: empty bucket → NO bar; a partial session aggregates the
  bars present.
* expiry partial-day: a short last-day session aggregates whatever 1m exists.
* forming-session ``before`` guard drops today's incomplete session.
* corrected_point #5: real OI all session, null ONLY on the final bar → daily OI null.
* corrected_point #3: a frame WITHOUT an ``open_interest`` column → daily OI None.
* the positional ``(frame, before)`` adapter used by the shared daily driver.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import polars as pl
from brutex.data.fno_daily_resample import (
    resample_1m_to_fno_daily,
    resample_1m_to_fno_daily_positional,
)

_IST = timezone(timedelta(hours=5, minutes=30))
# 09:15 IST == 03:45 UTC.
_SESSION_OPEN_UTC = datetime(2026, 5, 13, 3, 45, tzinfo=UTC)
# The forming-session cutoff: a date strictly after the session so it is COMPLETE.
_AFTER = date(2026, 5, 14)


def _1m_frame(
    *,
    timestamps: list[datetime],
    closes: list[float],
    ois: list[int | None],
    with_oi: bool = True,
) -> pl.DataFrame:
    n = len(timestamps)
    data: dict[str, object] = {
        "ts_utc": timestamps,
        "open": [10.0] * n,
        "high": [c + 1.0 for c in closes],
        "low": [9.0] * n,
        "close": closes,
        "volume": [100] * n,
    }
    schema: dict[str, pl.DataType] = {
        "ts_utc": pl.Datetime(time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
    }
    if with_oi:
        data["open_interest"] = ois
        schema["open_interest"] = pl.Int64
    return pl.DataFrame(data, schema=schema)


class TestOiCarryLast:
    def test_daily_oi_is_last_of_day_not_sum(self) -> None:
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(5)],
            closes=[11.0, 12.0, 13.0, 14.0, 15.0],
            ois=[100, 120, 90, 80, 70],
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        assert len(bars) == 1
        assert bars[0].open_interest == 70  # last-of-day, NOT 460 (sum) / 100 / 120

    def test_daily_bar_is_stamped_midnight_ist(self) -> None:
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC],
            closes=[11.0],
            ois=[55],
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        # midnight 2026-05-13 IST == 2026-05-12 18:30 UTC.
        assert bars[0].timestamp == datetime(2026, 5, 13, tzinfo=_IST).astimezone(UTC)
        assert bars[0].timestamp == datetime(2026, 5, 12, 18, 30, tzinfo=UTC)


class TestSparseAndPartialSessions:
    def test_sparse_minute_gap_aggregates_present_bars_only(self) -> None:
        # Only 2 of a would-be 5-bar window present (a mid-session illiquid gap):
        # the daily bar aggregates the 2 bars; OI is the last PRESENT bar's OI.
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC, _SESSION_OPEN_UTC + timedelta(minutes=3)],
            closes=[11.0, 14.0],
            ois=[100, 88],
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        assert len(bars) == 1
        assert bars[0].volume == 200
        assert bars[0].open_interest == 88

    def test_empty_frame_emits_no_bar(self) -> None:
        empty = _1m_frame(timestamps=[], closes=[], ois=[])
        assert resample_1m_to_fno_daily(empty, before=_AFTER) == []

    def test_expiry_partial_last_day_session_aggregates_what_exists(self) -> None:
        # An expired contract's last day has only a short session (3 early bars) —
        # the derive aggregates exactly those, never fabricating a full session.
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(3)],
            closes=[11.0, 12.0, 13.0],
            ois=[200, 210, 205],
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        assert len(bars) == 1
        assert bars[0].close == 13  # last present minute's close
        assert bars[0].open_interest == 205


class TestFormingSessionGuard:
    def test_today_forming_session_is_dropped(self) -> None:
        # The session's IST date EQUALS ``before`` (today) → dropped, no partial bar.
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC],
            closes=[11.0],
            ois=[10],
        )
        # before == the session's IST date (2026-05-13) → forming → dropped.
        assert resample_1m_to_fno_daily(df, before=date(2026, 5, 13)) == []


class TestTrailingNullOi:
    def test_real_oi_all_session_null_only_final_bar_yields_null_daily_oi(self) -> None:
        # corrected_point #5: real OI all session, null ONLY on the FINAL bar →
        # the carry-last rule yields a NULL daily OI (honest, no fabrication).
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(5)],
            closes=[11.0, 12.0, 13.0, 14.0, 15.0],
            ois=[100, 110, 120, 130, None],
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        assert len(bars) == 1
        assert bars[0].open_interest is None


class TestDefensiveOiColumnAbsent:
    def test_frame_without_open_interest_column_yields_none_oi(self) -> None:
        # corrected_point #3: a hand-built frame with NO open_interest column must not
        # KeyError — the daily OI is honestly None.
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC],
            closes=[11.0],
            ois=[],
            with_oi=False,
        )
        bars = resample_1m_to_fno_daily(df, before=_AFTER)
        assert len(bars) == 1
        assert bars[0].open_interest is None


class TestPositionalAdapter:
    def test_positional_adapter_matches_keyword_form(self) -> None:
        df = _1m_frame(
            timestamps=[_SESSION_OPEN_UTC],
            closes=[11.0],
            ois=[42],
        )
        kw = resample_1m_to_fno_daily(df, before=_AFTER)
        pos = resample_1m_to_fno_daily_positional(df, _AFTER)
        assert kw == pos
