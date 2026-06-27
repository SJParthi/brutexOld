"""Golden (byte-pinned) OI carry-last fixtures (DEC-FNO-TF-DERIVE-OI-001).

Pins the EXACT derived values for a hand-built F&O 1-minute sequence so the
carry-last OI rule (NOT sum / first / max) can never silently regress:
* a 5-minute intraday bucket → OI == the LAST source bar's OI;
* the 1-day derive → OI == the LAST bar of the session's OI (last-of-day).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily
from brutex.data.tf_resampler import resample_1m_to_tf

# 09:15 IST == 03:45 UTC.
_SESSION_OPEN_UTC = datetime(2026, 5, 13, 3, 45, tzinfo=UTC)

# A hand-built F&O 1-minute sequence: OI rises then falls within a single 5m bucket.
_OI_SEQUENCE = [100, 120, 90, 110, 95]
_CLOSES = [11.0, 12.5, 11.5, 13.0, 12.0]


def _golden_5m_bucket_frame() -> pl.DataFrame:
    n = len(_OI_SEQUENCE)
    return pl.DataFrame(
        {
            "ts_utc": [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)],
            "open": [10.0, 11.0, 12.0, 11.0, 13.0],
            "high": [11.0, 13.0, 12.5, 13.5, 12.5],
            "low": [9.5, 10.5, 11.0, 10.5, 11.5],
            "close": _CLOSES,
            "volume": [100, 200, 150, 175, 125],
            "open_interest": _OI_SEQUENCE,
        },
        schema={
            "ts_utc": pl.Datetime(time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    )


def test_5m_bucket_oi_is_last_not_sum_first_or_max() -> None:
    out = resample_1m_to_tf(_golden_5m_bucket_frame(), 5)
    assert out.height == 1
    # The frozen golden answer-key for this bucket:
    assert out.get_column("open").to_list() == [10.0]  # first
    assert out.get_column("high").to_list() == [13.5]  # max
    assert out.get_column("low").to_list() == [9.5]  # min
    assert out.get_column("close").to_list() == [12.0]  # last
    assert out.get_column("volume").to_list() == [750]  # sum
    # OI is the LAST source bar's value (95) — NOT the sum (515), first (100),
    # or max (120). This is the load-bearing carry-last assertion.
    assert out.get_column("open_interest").to_list() == [95]


def test_1day_derive_oi_is_last_of_day() -> None:
    bars = resample_1m_to_fno_daily(_golden_5m_bucket_frame(), before=date(2026, 5, 14))
    assert len(bars) == 1
    bar = bars[0]
    # midnight 2026-05-13 IST == 2026-05-12 18:30 UTC (the frozen daily stamp).
    assert bar.timestamp == datetime(2026, 5, 12, 18, 30, tzinfo=UTC)
    assert str(bar.open) == "10.0"
    assert str(bar.high) == "13.5"
    assert str(bar.low) == "9.5"
    assert str(bar.close) == "12.0"
    assert bar.volume == 750
    # last-of-day OI (95), NOT the sum / first / max.
    assert bar.open_interest == 95
