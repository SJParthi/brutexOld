"""Frozen-oracle byte-parity + registry-config resampler tests
(DEC-TF-REGISTRY-DYNAMIC-001).

``_legacy_minute_bucket_resample`` is a VERBATIM frozen copy of the
pre-registry minute bucket arithmetic (the oracle, from tf_resampler.py @
origin/main ecb37cc). The parity suites prove the seconds-keyed path is
frame-identical for all 21 default timeframes over hostile fixtures
(pre-open, post-close, odd minutes, month boundary) and under
hypothesis-generated whole-minute frames. The config suites prove a NEW 20m
timeframe works via one TfSpec, and that a 30-second spec buckets
second-stamped input correctly — a FRAMEWORK proof on synthetic data (no
production sub-minute feed exists; Groww is 1-minute-only).
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest
from brutex.data.tf_resampler import resample_1m_to_tf, resample_frame_to_spec
from brutex.data.timeframes import ALL_TF_MINUTES, TfAnchor, TfSpec
from hypothesis import given
from hypothesis import strategies as st

_UTC = dt.UTC


def _legacy_minute_bucket_resample(df_1m: pl.DataFrame, target_tf_minutes: int) -> pl.DataFrame:
    """VERBATIM frozen pre-DEC-TF-REGISTRY-DYNAMIC-001 minute bucket math (the oracle).

    OHLCV arm only — the fixtures below are OHLCV-only; the OI/Greeks
    carry-last arms are byte-untouched by the registry change and stay
    covered by their own unmodified suites.
    """
    ist = pl.col("ts_utc") + pl.duration(hours=5, minutes=30)
    session_open_ist = ist.dt.truncate("1d") + pl.duration(hours=9, minutes=15)
    if target_tf_minutes < 375:
        offset_minutes = (ist - session_open_ist).dt.total_minutes()
        bucket_minutes = (offset_minutes // target_tf_minutes) * target_tf_minutes
        bucket_utc = (session_open_ist + pl.duration(minutes=bucket_minutes)) - pl.duration(
            hours=5, minutes=30
        )
    else:
        bucket_utc = session_open_ist - pl.duration(hours=5, minutes=30)
    aggregations = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    return (
        df_1m.with_columns(bucket_utc.alias("_bucket_ts"))
        .group_by("_bucket_ts", maintain_order=True)
        .agg(*aggregations)
        .rename({"_bucket_ts": "ts_utc"})
        .sort("ts_utc")
    )


def _frame(stamps: list[dt.datetime]) -> pl.DataFrame:
    """Deterministic OHLCV frame over *stamps* (strictly increasing)."""
    n = len(stamps)
    return pl.DataFrame(
        {
            "ts_utc": stamps,
            "open": [100.0 + i for i in range(n)],
            "high": [102.5 + 2 * i for i in range(n)],
            "low": [99.0 - i for i in range(n)],
            "close": [101.0 + 1.5 * i for i in range(n)],
            "volume": [10 * (i + 1) for i in range(n)],
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))


def _ist(y: int, mo: int, d: int, h: int, m: int, s: int = 0) -> dt.datetime:
    """The UTC instant of an IST wall-clock time (IST = UTC+05:30, no DST)."""
    return dt.datetime(y, mo, d, h, m, s, tzinfo=_UTC) - dt.timedelta(hours=5, minutes=30)


#: Hostile fixture: two IST days straddling a month boundary; pre-open 09:00,
#: session open 09:15, odd minutes, the 15:29 close edge, post-close 15:35.
_HOSTILE_STAMPS = [
    _ist(2024, 1, 31, 9, 0),
    _ist(2024, 1, 31, 9, 15),
    _ist(2024, 1, 31, 9, 16),
    _ist(2024, 1, 31, 10, 47),
    _ist(2024, 1, 31, 12, 0),
    _ist(2024, 1, 31, 15, 29),
    _ist(2024, 1, 31, 15, 35),
    _ist(2024, 2, 1, 9, 15),
    _ist(2024, 2, 1, 11, 3),
    _ist(2024, 2, 1, 15, 29),
]


class TestFrozenOracleParity:
    """The seconds-keyed path ≡ the frozen minute-math oracle, all 21 TFs."""

    @pytest.mark.parametrize("tf", ALL_TF_MINUTES)
    def test_hostile_fixture_parity(self, tf: int) -> None:
        df = _frame(_HOSTILE_STAMPS)
        assert resample_1m_to_tf(df, tf).equals(_legacy_minute_bucket_resample(df, tf))

    @given(
        offsets=st.lists(
            st.integers(min_value=-30, max_value=2000),
            min_size=1,
            max_size=40,
            unique=True,
        )
    )
    def test_hypothesis_whole_minute_parity(self, offsets: list[int]) -> None:
        base = _ist(2024, 6, 3, 9, 15)
        stamps = [base + dt.timedelta(minutes=o) for o in sorted(offsets)]
        df = _frame(stamps)
        for tf in ALL_TF_MINUTES:
            assert resample_1m_to_tf(df, tf).equals(_legacy_minute_bucket_resample(df, tf))


class TestNewTfByConfig20m:
    """A 20m timeframe works via ONE TfSpec — no resampler edit needed."""

    def test_20m_buckets_hand_derived(self) -> None:
        spec = TfSpec(
            label="20m",
            duration_s=1_200,
            base_granularity_s=60,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="20minute",
        )
        stamps = [
            _ist(2024, 6, 3, 9, 15),
            _ist(2024, 6, 3, 9, 34),
            _ist(2024, 6, 3, 9, 35),
            _ist(2024, 6, 3, 10, 0),
        ]
        out = resample_frame_to_spec(_frame(stamps), spec)
        assert out.get_column("ts_utc").to_list() == [
            _ist(2024, 6, 3, 9, 15),
            _ist(2024, 6, 3, 9, 35),
            _ist(2024, 6, 3, 9, 55),
        ]
        assert out.get_column("open").to_list() == [100.0, 102.0, 103.0]
        assert out.get_column("high").to_list() == [104.5, 106.5, 108.5]
        assert out.get_column("low").to_list() == [98.0, 97.0, 96.0]
        assert out.get_column("close").to_list() == [102.5, 104.0, 105.5]
        assert out.get_column("volume").to_list() == [30, 30, 40]


class TestSubMinuteFramework30s:
    """FRAMEWORK proof: a 30-second spec buckets second-stamped synthetic input.

    No production feed emits sub-minute bars (Groww is 1-minute-only) — this
    proves the seconds-keyed grid, NOT a production sub-minute capability.
    """

    def test_30s_buckets_second_stamped_input(self) -> None:
        spec = TfSpec(
            label="30s",
            duration_s=30,
            base_granularity_s=1,
            anchor=TfAnchor.SESSION_OPEN,
            lake_label="30second",
        )
        stamps = [
            _ist(2024, 6, 3, 9, 15, 0),
            _ist(2024, 6, 3, 9, 15, 10),
            _ist(2024, 6, 3, 9, 15, 30),
            _ist(2024, 6, 3, 9, 15, 59),
        ]
        out = resample_frame_to_spec(_frame(stamps), spec)
        assert out.get_column("ts_utc").to_list() == [
            _ist(2024, 6, 3, 9, 15, 0),
            _ist(2024, 6, 3, 9, 15, 30),
        ]
        assert out.get_column("open").to_list() == [100.0, 102.0]
        assert out.get_column("high").to_list() == [104.5, 108.5]
        assert out.get_column("low").to_list() == [98.0, 96.0]
        assert out.get_column("close").to_list() == [102.5, 105.5]
        assert out.get_column("volume").to_list() == [30, 70]


class TestResampleFrameToSpecContract:
    _FIVE = TfSpec(
        label="5m",
        duration_s=300,
        base_granularity_s=60,
        anchor=TfAnchor.SESSION_OPEN,
        lake_label="5minute",
    )

    def test_validates_input_type(self) -> None:
        with pytest.raises(TypeError, match="Expected polars.DataFrame"):
            resample_frame_to_spec("not a frame", self._FIVE)

    def test_daily_spec_matches_legacy_daily(self) -> None:
        df = _frame(_HOSTILE_STAMPS)
        daily = TfSpec(
            label="1d",
            duration_s=22_500,
            base_granularity_s=60,
            anchor=TfAnchor.IST_DAY,
            lake_label="1day",
        )
        assert resample_frame_to_spec(df, daily).equals(resample_1m_to_tf(df, 375))

    def test_minute_spec_matches_legacy_path(self) -> None:
        df = _frame(_HOSTILE_STAMPS)
        assert resample_frame_to_spec(df, self._FIVE).equals(resample_1m_to_tf(df, 5))
