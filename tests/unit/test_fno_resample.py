"""Unit tests for ``brutex.data.fno_resample`` (DEC-FNO-TF-DERIVE-OI-001).

Covers:
* :func:`derive_fno_intraday_tfs` — 19 intraday TFs, OI carried, 1m/1d excluded.
* :func:`resample_session_to_tf` — the on-the-fly /movers read path: the
  ``timestamp -> ts_utc`` rename (corrected_point #2), carry-last OI, and the
  trailing forming-bucket guard (corrected_point #1a).
* The unit twin of the resampler's OI carry-last vs the byte-identical spot
  6-column back-compat output.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
from brutex.data.fno_resample import derive_fno_intraday_tfs, resample_session_to_tf
from brutex.data.tf_resampler import resample_1m_to_tf

# 09:15 IST == 03:45 UTC (fixed +05:30, no DST).
_SESSION_OPEN_UTC = datetime(2026, 5, 13, 3, 45, tzinfo=UTC)


def _fno_1m(
    *,
    n: int,
    ts_col: str = "ts_utc",
    ois: list[int | None] | None = None,
) -> pl.DataFrame:
    """A synthetic F&O 1-minute frame (with ``open_interest``) of *n* consecutive bars."""
    timestamps = [_SESSION_OPEN_UTC + timedelta(minutes=i) for i in range(n)]
    closes = [11.0 + i for i in range(n)]
    interest = ois if ois is not None else [100 + i for i in range(n)]
    return pl.DataFrame(
        {
            ts_col: timestamps,
            "open": [10.0] * n,
            "high": [12.0 + i for i in range(n)],
            "low": [9.0] * n,
            "close": closes,
            "volume": [100] * n,
            "open_interest": interest,
        },
        schema={
            ts_col: pl.Datetime(time_zone="UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "open_interest": pl.Int64,
        },
    )


class TestDeriveFnoIntradayTfs:
    def test_returns_19_intraday_tfs_excluding_1m_and_1d(self) -> None:
        tfs = derive_fno_intraday_tfs(_fno_1m(n=20))
        assert len(tfs) == 19
        assert "1m" not in tfs
        assert "1d" not in tfs
        # The canonical intraday labels are present.
        assert {"2m", "5m", "15m", "30m", "60m", "240m"} <= set(tfs)

    def test_every_intraday_frame_carries_carry_last_oi(self) -> None:
        # OI [100..104] over 5 bars; the single 5m bucket carries the LAST (104).
        tfs = derive_fno_intraday_tfs(_fno_1m(n=5, ois=[100, 101, 102, 103, 104]))
        assert tfs["5m"].get_column("open_interest").to_list() == [104]
        # Volume is SUMMED (flow), unlike OI (stock) — 5 × 100.
        assert tfs["5m"].get_column("volume").to_list() == [500]


class TestResampleSessionToTf:
    def test_renames_lake_timestamp_to_ts_utc(self) -> None:
        # The lake column is named ``timestamp``; the resampler needs ``ts_utc``.
        out = resample_session_to_tf(_fno_1m(n=5, ts_col="timestamp"), 5)
        assert "ts_utc" in out.columns
        assert "timestamp" not in out.columns

    def test_carry_last_oi_over_a_5m_bucket(self) -> None:
        out = resample_session_to_tf(
            _fno_1m(n=5, ts_col="timestamp", ois=[100, 120, 90, 80, 70]), 5
        )
        assert out.get_column("open_interest").to_list() == [70]

    def test_trailing_partial_bucket_dropped_when_cutoff_mid_bucket(self) -> None:
        # 8 bars 09:15..09:22 IST. 5m grid → [09:15,09:20) closes 09:20, [09:20,09:25)
        # closes 09:25. Cutoff 09:22 (minute-of-day 562): the second bucket closes at
        # 09:25 (565 > 562) → DROPPED; only the first fully-closed bucket survives.
        df = _fno_1m(n=8, ts_col="timestamp")
        out = resample_session_to_tf(df, 5, cutoff_min=9 * 60 + 22)
        assert out.height == 1
        # The kept bucket is labelled at 09:15 IST == 03:45 UTC.
        assert out.get_column("ts_utc").to_list() == [_SESSION_OPEN_UTC]

    def test_cutoff_at_bucket_close_keeps_the_bucket(self) -> None:
        # Cutoff EXACTLY at a bucket close (09:20) keeps the [09:15,09:20) bucket
        # (``bucket_start + M <= cutoff`` is inclusive).
        df = _fno_1m(n=8, ts_col="timestamp")
        out = resample_session_to_tf(df, 5, cutoff_min=9 * 60 + 20)
        assert out.height == 1

    def test_cutoff_none_keeps_every_bucket(self) -> None:
        df = _fno_1m(n=8, ts_col="timestamp")
        out = resample_session_to_tf(df, 5, cutoff_min=None)
        assert out.height == 2

    def test_empty_frame_returns_empty(self) -> None:
        empty = _fno_1m(n=0, ts_col="timestamp")
        assert resample_session_to_tf(empty, 5).is_empty()

    def test_cutoff_below_first_bucket_close_drops_all(self) -> None:
        # A resampled-but-fully-forming day: cutoff 09:17 lands inside the only bucket
        # ([09:15,09:20) closes 09:20 > 557) → the whole frame is dropped (empty, not
        # a partial bar — corrected_point #1a no-look-ahead).
        df = _fno_1m(n=5, ts_col="timestamp")
        out = resample_session_to_tf(df, 5, cutoff_min=9 * 60 + 17)
        assert out.is_empty()


class TestResamplerBackCompat:
    def test_spot_no_oi_output_is_byte_identical_six_columns(self) -> None:
        # A spot frame (no OI column) resamples to the OHLCV-6, no OI added.
        fno = _fno_1m(n=5)
        spot = fno.drop("open_interest")
        out = resample_1m_to_tf(spot, 5)
        assert out.columns == ["ts_utc", "open", "high", "low", "close", "volume"]
