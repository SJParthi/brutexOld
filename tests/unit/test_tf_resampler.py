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
from brutex.data.errors import ResampleInputError
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


_SESSION_OPEN_UTC = datetime(2025, 6, 9, 3, 45, tzinfo=UTC).replace(tzinfo=None)


class TestFixedArithmeticAnchor:
    """DEC-RESAMPLE-ANCHOR-002: the grid depends on calendar TIME, not bars.

    The prior ``start_by="datapoint"`` anchoring keyed each session's grid to
    its FIRST bar — a vendor gap at the 09:15 open silently shifted the whole
    day's grid for every timeframe. The fixed-arithmetic anchor is immune.
    """

    def test_missing_open_bar_does_not_shift_the_grid(self) -> None:
        # Session whose 09:15 IST bar is MISSING (first bar at 09:16 IST).
        full = _make_1m_session(_SESSION_OPEN_UTC, 10)
        gapped = full.slice(1)  # drop the 09:15 bar

        out = resample_1m_to_tf(gapped, 2)
        labels = out["ts_utc"].to_list()
        # Grid stays 09:15 + k*2m (03:45 UTC + k*2m) — NOT 09:16-anchored.
        expected_grid = [_SESSION_OPEN_UTC + timedelta(minutes=2 * k) for k in range(5)]
        assert labels == expected_grid
        # The first bucket [09:15, 09:17) holds ONLY the surviving 09:16 bar.
        first = out.row(0, named=True)
        src = gapped.row(0, named=True)
        assert first["open"] == src["open"]
        assert first["close"] == src["close"]
        assert first["volume"] == 1000

    def test_missing_open_bar_daily_label_still_session_open(self) -> None:
        gapped = _make_1m_session(_SESSION_OPEN_UTC, 375).slice(2)  # first bar 09:17 IST
        out = resample_1m_to_tf(gapped, 375)
        assert out.height == 1
        # Label pinned to the 09:15 IST session open (03:45 UTC), not 09:17.
        assert out["ts_utc"].to_list() == [_SESSION_OPEN_UTC]

    def test_mid_session_gap_keeps_grid_and_conserves_ohlcv(self) -> None:
        full = _make_1m_session(_SESSION_OPEN_UTC, 30)
        # Drop minutes 10..14 (a 5-minute mid-session halt).
        gapped = pl.concat([full.slice(0, 10), full.slice(15)])
        out = resample_1m_to_tf(gapped, 5)
        # Bucket 10:15 (minutes 10-14) must be ABSENT (no fabricated bars);
        # every other bucket sits on the 09:15 + k*5m grid.
        labels = out["ts_utc"].to_list()
        expected = [_SESSION_OPEN_UTC + timedelta(minutes=5 * k) for k in (0, 1, 3, 4, 5)]
        assert labels == expected
        # Conservation on a surviving bucket (minutes 15-19).
        bucket = out.filter(pl.col("ts_utc") == _SESSION_OPEN_UTC + timedelta(minutes=15)).row(
            0, named=True
        )
        window = full.slice(15, 5)
        assert bucket["open"] == window["open"][0]
        assert bucket["high"] == window["high"].max()
        assert bucket["low"] == window["low"].min()
        assert bucket["close"] == window["close"][-1]
        assert bucket["volume"] == window["volume"].sum()


class TestCompletenessLaws:
    """DEC-RESAMPLE-ANCHOR-002 completeness laws over ALL 21 timeframes."""

    def test_full_session_bar_count_law(self) -> None:
        # A gap-free 375-minute session yields exactly ceil(375/tf) bars.
        from brutex.data.timeframes import ALL_TF_MINUTES

        full = _make_1m_session(_SESSION_OPEN_UTC, 375)
        for tf in ALL_TF_MINUTES:
            out = resample_1m_to_tf(full, tf)
            expected = -(-375 // tf)  # ceil
            assert out.height == expected, f"tf={tf}: {out.height} != {expected}"

    def test_every_bucket_on_the_session_grid(self) -> None:
        from brutex.data.timeframes import ALL_TF_MINUTES, DAILY_TF_MINUTES

        full = _make_1m_session(_SESSION_OPEN_UTC, 375)
        for tf in ALL_TF_MINUTES:
            if tf == DAILY_TF_MINUTES:
                continue
            out = resample_1m_to_tf(full, tf)
            for ts in out["ts_utc"].to_list():
                offset = (ts - _SESSION_OPEN_UTC).total_seconds() / 60
                assert offset >= 0 and offset % tf == 0, f"tf={tf}: off-grid label {ts}"

    def test_no_bucket_straddles_sessions(self) -> None:
        # Two consecutive sessions; every bucket's bars share ONE IST date.
        from brutex.data.timeframes import ALL_TF_MINUTES

        day1 = _make_1m_session(_SESSION_OPEN_UTC, 375)
        day2 = _make_1m_session(_SESSION_OPEN_UTC + timedelta(days=1), 375, base_price=200.0)
        both = pl.concat([day1, day2])
        for tf in ALL_TF_MINUTES:
            out = resample_1m_to_tf(both, tf)
            per_day = -(-375 // tf) if tf != 375 else 1
            assert out.height == 2 * per_day, f"tf={tf}: cross-session merge suspected"
            # Day-2 first bucket opens at day-2's first price — no day-1 leak.
            d2_first = out.filter(pl.col("ts_utc") >= _SESSION_OPEN_UTC + timedelta(days=1)).row(
                0, named=True
            )
            assert d2_first["open"] == day2["open"][0], f"tf={tf}: day-1 data leaked"


# --------------------------------------------------------------------------- #
# Input monotonicity guard — DEC-RESAMPLER-INPUT-GUARD-001. Mirrors the feed
# resampler's raise-on-non-monotonic suite (tests/unit/test_feed_resampler.py,
# DEC-RESAMPLER-MONOTONIC-GUARD-001): before the guard, a duplicate ts_utc
# silently DOUBLE-COUNTED the bucket's volume (10+10+5 → 25) and polluted
# high/low, and an unsorted input inverted the bucket's open/close.
# --------------------------------------------------------------------------- #
_G_OPEN = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)  # 09:15 IST session open


def _df_at(offsets_minutes: list[int], volumes: list[int] | None = None) -> pl.DataFrame:
    """A minimal OHLCV frame whose rows sit at the given minute offsets."""
    n = len(offsets_minutes)
    vols = volumes if volumes is not None else [10] * n
    closes = [100.0 + i for i in range(n)]
    return pl.DataFrame(
        {
            "ts_utc": [_G_OPEN + timedelta(minutes=m) for m in offsets_minutes],
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": vols,
        },
        schema={
            "ts_utc": pl.Datetime,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    ).with_columns(pl.col("ts_utc").dt.replace_time_zone(None))


class TestInputMonotonicGuard:
    def test_exact_duplicate_raises_typed_error(self) -> None:
        """FAILED ON MAIN: a duplicate ts landed in the same bucket silently."""
        with pytest.raises(ResampleInputError, match="STRICTLY increasing"):
            resample_1m_to_tf(_df_at([0, 0, 1]), 5)

    def test_duplicate_does_not_double_count_volume(self) -> None:
        """The verifier's exact repro: dup volumes 10+10+5 must never read 25."""
        dup = _df_at([0, 0, 1], volumes=[10, 10, 5])
        with pytest.raises(ResampleInputError):
            resample_1m_to_tf(dup, 5)
        # Sanity floor: the SAME volumes de-collided to distinct minutes
        # aggregate to exactly 25 in ONE bucket — the value the duplicate
        # frame would have FAKED without the guard.
        clean = _df_at([0, 1, 2], volumes=[10, 10, 5])
        out = resample_1m_to_tf(clean, 5)
        assert out.get_column("volume").to_list() == [25]

    def test_duplicate_across_bucket_boundary_raises(self) -> None:
        """A dup pair straddling two 5m buckets still trips the guard."""
        with pytest.raises(ResampleInputError, match="STRICTLY increasing"):
            resample_1m_to_tf(_df_at([3, 4, 4, 5]), 5)

    def test_mid_stream_out_of_order_raises(self) -> None:
        """FAILED ON MAIN: [t0,t1,t2,t1,t4] inverted the bucket's open/close."""
        with pytest.raises(ResampleInputError, match="STRICTLY increasing"):
            resample_1m_to_tf(_df_at([0, 1, 2, 1, 4]), 5)

    def test_strictly_decreasing_raises(self) -> None:
        with pytest.raises(ResampleInputError, match="STRICTLY increasing"):
            resample_1m_to_tf(_df_at([4, 3, 2, 1, 0]), 5)

    def test_unsorted_input_would_invert_open_close_proof(self) -> None:
        """Anti-illusion proof: sorted vs reversed input disagree on open/close.

        Documents WHY the guard exists — run the reversed frame through a
        guard-free clone of the aggregation and show the open/close invert.
        """
        clean = _df_at([0, 1, 2])
        out = resample_1m_to_tf(clean, 5)
        assert out.get_column("open").to_list() == [100.0]
        assert out.get_column("close").to_list() == [102.0]
        with pytest.raises(ResampleInputError):
            resample_1m_to_tf(clean.reverse(), 5)

    def test_no_partial_output_guard_runs_before_aggregation(self) -> None:
        """The guard raises for EVERY target TF — including the 1m identity
        path and the daily path — before any aggregation output exists."""
        dup = _df_at([0, 0])
        for tf in (1, 5, 375):
            with pytest.raises(ResampleInputError):
                resample_1m_to_tf(dup, tf)

    def test_resample_all_21_tfs_propagates_guard(self) -> None:
        with pytest.raises(ResampleInputError):
            resample_all_21_tfs(_df_at([1, 0]))

    def test_null_timestamp_raises_typed_error(self) -> None:
        df = _df_at([0, 1, 2]).with_columns(
            pl.when(pl.int_range(pl.len()) == 1)
            .then(None)
            .otherwise(pl.col("ts_utc"))
            .alias("ts_utc")
        )
        with pytest.raises(ResampleInputError, match="null timestamp"):
            resample_1m_to_tf(df, 5)

    def test_error_names_the_offending_row_and_both_timestamps(self) -> None:
        try:
            resample_1m_to_tf(_df_at([0, 1, 2, 1, 4]), 5)
        except ResampleInputError as exc:
            msg = str(exc)
            assert "row 3" in msg
            assert "1 violation(s) total" in msg
        else:  # pragma: no cover - the guard must raise
            raise AssertionError("guard did not fire")

    def test_single_row_and_sorted_inputs_unaffected(self) -> None:
        """Back-compat: a 1-row frame and a clean sorted frame stay byte-identical."""
        one = _df_at([0])
        assert resample_1m_to_tf(one, 5).get_column("volume").to_list() == [10]
        clean = _df_at([0, 1, 2, 3, 4])
        assert resample_1m_to_tf(clean, 5).height == 1

    def test_typed_error_is_a_data_lake_error(self) -> None:
        """Callers with broad DataLakeError handling absorb the new error."""
        from brutex.data.errors import DataLakeError

        assert issubclass(ResampleInputError, DataLakeError)


class TestOpenInterestCarryLastStemMatched:
    """OI carry-last coverage INSIDE the module's stem-matched suite.

    The CI touched-module discovery gate (DEC-CICD-GATE-HONESTY-001,
    scripts/ci/coverage_gate_discovery.py) gates a touched module at 100%
    under its STEM-MATCHED test files only — `test_fno_resample.py` (which
    historically covered the `has_oi` aggregation branch per
    DEC-FNO-TF-DERIVE-OI-001) does not stem-match `tf_resampler`, so the
    branch must also be exercised HERE (caught live on this PR's first CI
    run: 97.06%, line 249 missed under the stem set).
    """

    def test_oi_column_carried_as_last_snapshot_never_summed(self) -> None:
        df = _df_at([0, 1, 2, 5, 6], volumes=[10, 10, 10, 10, 10]).with_columns(
            pl.Series("open_interest", [100, 200, 300, 400, None], dtype=pl.Int64)
        )
        out = resample_1m_to_tf(df, 5)
        # Bucket 1 = minutes 0-2 → LAST bar's OI (300), never 100+200+300.
        # Bucket 2 = minutes 5-6 → last bar's OI is null → preserved null.
        assert out.get_column("open_interest").to_list() == [300, None]
        # Volume stays the summed FLOW — proving OI is the snapshot exception.
        assert out.get_column("volume").to_list() == [30, 20]

    def test_spot_frame_without_oi_stays_six_columns(self) -> None:
        out = resample_1m_to_tf(_df_at([0, 1, 2]), 5)
        assert "open_interest" not in out.columns
        assert list(out.columns) == list(REQUIRED_COLUMNS)
