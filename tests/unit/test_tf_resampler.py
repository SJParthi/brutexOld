"""Tests for `brutex.data.tf_resampler` — Polars 1m → 21-TF resampler.

Synthetic-data tests covering:
- Schema validation
- Bucket math (5m bucket = 5 consecutive 1m bars aggregated correctly)
- All 21 TFs produce correct row counts for a known input
- OHLCV aggregation rules (first/max/min/last/sum)
- 1m -> 1m is no-op
- Daily TF aggregates whole trading session
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
from brutex.data.tf_resampler import (
    REQUIRED_COLUMNS,
    resample_1m_to_tf,
    resample_all_21_tfs,
)


def _make_1m_session(
    session_open_utc: datetime,
    n_bars: int,
    base_price: float = 100.0,
) -> pl.DataFrame:
    """Build a synthetic 1m bar DataFrame for testing.

    Each bar increments by 0.1 in close.  High = close + 0.05, Low = close - 0.05.
    Volume = 1000 per bar.
    """
    timestamps = [session_open_utc + timedelta(minutes=i) for i in range(n_bars)]
    closes = [base_price + i * 0.1 for i in range(n_bars)]
    return pl.DataFrame(
        {
            "ts_utc": timestamps,
            "open": [base_price + i * 0.1 - 0.05 for i in range(n_bars)],
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1000] * n_bars,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


class TestSchemaValidation:
    def test_non_dataframe_input_raises_typeerror(self) -> None:
        """Cover line 77: non-DataFrame input raises TypeError with the
        type name in the message."""
        with pytest.raises(TypeError, match="Expected polars.DataFrame"):
            resample_1m_to_tf([1, 2, 3], 5)  # type: ignore[arg-type]

    def test_missing_column_raises(self) -> None:
        df = pl.DataFrame(
            {
                "ts_utc": [datetime(2026, 5, 13, 3, 45, tzinfo=UTC)],
                "open": [100.0],
                # missing high, low, close, volume
            }
        )
        with pytest.raises(ValueError, match="missing required columns"):
            resample_1m_to_tf(df, 5)

    def test_non_datetime_ts_raises(self) -> None:
        df = pl.DataFrame(
            {
                "ts_utc": ["2026-05-13"],  # string, not datetime
                "open": [100.0],
                "high": [100.0],
                "low": [100.0],
                "close": [100.0],
                "volume": [1000],
            }
        )
        with pytest.raises(ValueError, match="must be polars.Datetime"):
            resample_1m_to_tf(df, 5)

    def test_invalid_tf_raises(self) -> None:
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=5)
        with pytest.raises(ValueError, match="Unsupported target TF"):
            resample_1m_to_tf(df, 7777)


class TestNoOpFor1m:
    def test_1m_to_1m_returns_same_rows(self) -> None:
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=10)
        result = resample_1m_to_tf(df, 1)
        assert result.height == 10
        assert result.columns == df.columns


class TestBucketMath:
    def test_5m_bucket_aggregates_5_bars(self) -> None:
        # 10 bars at 1m -> should produce 2 bars at 5m
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=10)
        result = resample_1m_to_tf(df, 5)
        assert result.height == 2

    def test_5m_bucket_open_first_close_last(self) -> None:
        # Bars 0-4 close: 100.0, 100.1, 100.2, 100.3, 100.4
        # 5m bucket: open=99.95 (first), high=100.45 (max), low=99.95 (min), close=100.4 (last)
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=5)
        result = resample_1m_to_tf(df, 5)
        assert result.height == 1
        row = result.row(0, named=True)
        assert abs(row["open"] - 99.95) < 1e-9
        assert abs(row["close"] - 100.4) < 1e-9

    def test_5m_bucket_high_is_max(self) -> None:
        # Build bars where high[2] is the explicit max in the bucket
        ts = datetime(2026, 5, 13, 3, 45)
        df = pl.DataFrame(
            {
                "ts_utc": [ts + timedelta(minutes=i) for i in range(5)],
                "open": [100.0, 100.0, 100.0, 100.0, 100.0],
                "high": [100.5, 100.6, 105.0, 100.4, 100.3],  # max=105.0
                "low": [99.5, 99.6, 99.7, 99.4, 99.3],  # min=99.3
                "close": [100.0, 100.0, 100.0, 100.0, 100.0],
                "volume": [1000, 1000, 1000, 1000, 1000],
            },
            schema={
                "ts_utc": pl.Datetime,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
            },
        )
        result = resample_1m_to_tf(df, 5)
        assert result.height == 1
        row = result.row(0, named=True)
        assert row["high"] == 105.0
        assert row["low"] == 99.3

    def test_5m_bucket_volume_is_sum(self) -> None:
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=5)
        result = resample_1m_to_tf(df, 5)
        assert result.height == 1
        assert result.row(0, named=True)["volume"] == 5 * 1000

    def test_15m_bucket_aggregates_15_bars(self) -> None:
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=30)
        result = resample_1m_to_tf(df, 15)
        assert result.height == 2  # 30 bars = 2 × 15m


class TestAll21TFs:
    def test_resample_all_21_returns_21_dataframes(self) -> None:
        # Generate one full 375m session
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=375)
        all_tfs = resample_all_21_tfs(df)
        assert len(all_tfs) == 21

    def test_all_21_have_required_columns(self) -> None:
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=375)
        all_tfs = resample_all_21_tfs(df)
        for tf, df_tf in all_tfs.items():
            for col in REQUIRED_COLUMNS:
                assert col in df_tf.columns, f"TF {tf} missing column {col}"


class TestDailyTF:
    def test_daily_one_bar_per_session(self) -> None:
        # One full 375-bar session -> exactly 1 daily bar
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=375)
        result = resample_1m_to_tf(df, 375)
        assert result.height == 1

    def test_two_sessions_produce_two_daily_bars(self) -> None:
        # Day 1: session start 03:45 UTC = 09:15 IST, 375 bars
        # Day 2: session start next day 03:45 UTC, 375 bars
        day1_open = datetime(2026, 5, 13, 3, 45)
        day1 = _make_1m_session(day1_open, n_bars=375, base_price=100.0)
        day2_open = day1_open + timedelta(days=1)
        day2 = _make_1m_session(day2_open, n_bars=375, base_price=200.0)
        df_combined = pl.concat([day1, day2])
        result = resample_1m_to_tf(df_combined, 375)
        assert result.height == 2

    def test_daily_aggregation_correct(self) -> None:
        # 375 bars in one session, closes increment from 100.0
        # Expected daily: open=99.95 (first), close=100.0 + 374*0.1 - 0.0... actually
        # let's compute: close at bar 374 = 100.0 + 374 * 0.1 = 137.4
        # open at bar 0 = 99.95 (= 100.0 - 0.05 since open is base + i*0.1 - 0.05)
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=375)
        result = resample_1m_to_tf(df, 375)
        assert result.height == 1
        row = result.row(0, named=True)
        assert abs(row["close"] - 137.4) < 1e-9
        assert row["volume"] == 375 * 1000


class TestBucketTimestamps:
    def test_5m_bucket_timestamps_anchored_to_session_open(self) -> None:
        # 03:45 UTC = 09:15 IST is the session open
        session_open = datetime(2026, 5, 13, 3, 45)
        df = _make_1m_session(session_open, n_bars=10)
        result = resample_1m_to_tf(df, 5)
        # First 5m bucket starts at session open
        first_ts = result.row(0, named=True)["ts_utc"]
        # Polars Datetime comparison
        assert first_ts.replace(tzinfo=None) == session_open

    def _bucket_starts_ist(self, df: pl.DataFrame, tf: int) -> list[str]:
        """Return resampled bucket-start timestamps as HH:MM IST strings."""
        result = resample_1m_to_tf(df, tf)
        return [
            (row + timedelta(hours=5, minutes=30)).strftime("%H:%M")
            for row in result["ts_utc"].to_list()
        ]

    def test_2m_anchored_to_0915_not_0914(self) -> None:
        """DEC-RESAMPLE-ANCHOR-001 regression: 2m must start 09:15, not 09:14.

        Before the fix, group_by_dynamic anchored to the UTC epoch and the
        first 2m bucket landed at 09:14 IST (225 min from UTC-midnight is
        odd, so 09:15 was mid-bucket on the even-minute epoch grid).
        """
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=12)
        assert self._bucket_starts_ist(df, 2)[:4] == ["09:15", "09:17", "09:19", "09:21"]

    def test_4m_anchored_to_0915_not_0914(self) -> None:
        """DEC-RESAMPLE-ANCHOR-001 regression: 4m must start 09:15, not 09:14."""
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=16)
        assert self._bucket_starts_ist(df, 4)[:3] == ["09:15", "09:19", "09:23"]

    def test_3m_and_15m_still_anchored_after_fix(self) -> None:
        """The TFs that aligned by luck must still anchor to 09:15."""
        df = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=30)
        assert self._bucket_starts_ist(df, 3)[:3] == ["09:15", "09:18", "09:21"]
        assert self._bucket_starts_ist(df, 15)[:2] == ["09:15", "09:30"]

    def test_2m_reanchors_each_trading_day(self) -> None:
        """Multi-day input: every session re-anchors to its own 09:15.

        A naive global epoch grid would carry the misalignment across the
        overnight gap; per-day anchoring resets it.
        """
        day1 = _make_1m_session(datetime(2026, 5, 13, 3, 45), n_bars=6, base_price=100.0)
        day2 = _make_1m_session(datetime(2026, 5, 14, 3, 45), n_bars=6, base_price=200.0)
        df = pl.concat([day1, day2])
        result = resample_1m_to_tf(df, 2)
        stamps = [
            (row + timedelta(hours=5, minutes=30)).strftime("%m-%d %H:%M")
            for row in result["ts_utc"].to_list()
        ]
        assert stamps[:3] == ["05-13 09:15", "05-13 09:17", "05-13 09:19"]
        assert stamps[3:6] == ["05-14 09:15", "05-14 09:17", "05-14 09:19"]
