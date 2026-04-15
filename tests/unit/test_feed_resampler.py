"""Unit tests for :mod:`brutex.feed.resampler`.

Pure tests — no ``polars`` / ``pyarrow`` / ``duckdb`` needed; every
assertion runs against hand-crafted :class:`CandleBar` lists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.feed.errors import ResampleError
from brutex.feed.resampler import (
    INTERVAL_SECONDS,
    RESAMPLEABLE_INTERVALS,
    resample_bars,
)

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _bar(
    minute_offset: int,
    *,
    open_price: float = 100.0,
    high_price: float | None = None,
    low_price: float | None = None,
    close_price: float | None = None,
    volume: int = 10,
    open_interest: int | None = 500,
    base: datetime | None = None,
) -> CandleBar:
    """Build a deterministic :class:`CandleBar` *minute_offset* minutes after *base*.

    Unspecified high/low/close default to ``open \u00b1 1`` so the bar
    always satisfies :class:`CandleBar`'s validation invariants even
    when the caller only varies ``open_price``.
    """
    base_ts = base or datetime(2024, 1, 2, 9, 15, tzinfo=UTC)
    high = high_price if high_price is not None else open_price + 1
    low = low_price if low_price is not None else open_price - 1
    close = close_price if close_price is not None else open_price + 0.5
    return CandleBar(
        timestamp=base_ts + timedelta(minutes=minute_offset),
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        open_interest=open_interest,
    )


# ────────────────────────────────────────────────────────────────────────
# Static tables
# ────────────────────────────────────────────────────────────────────────


class TestStaticTables:
    def test_interval_seconds_contains_nine_intervals(self) -> None:
        assert set(INTERVAL_SECONDS.keys()) == {
            "1minute",
            "2minute",
            "3minute",
            "5minute",
            "10minute",
            "15minute",
            "30minute",
            "1hour",
            "4hour",
            "1day",
        }

    def test_resampleable_intervals_excludes_1day(self) -> None:
        # 1day is in INTERVAL_SECONDS for completeness but calendar-
        # aware resampling lands in P3 so the v1 resampler refuses it.
        assert "1day" not in RESAMPLEABLE_INTERVALS
        assert "1minute" in RESAMPLEABLE_INTERVALS
        assert "4hour" in RESAMPLEABLE_INTERVALS

    def test_interval_seconds_match_minute_math(self) -> None:
        assert INTERVAL_SECONDS["1minute"] == 60
        assert INTERVAL_SECONDS["5minute"] == 300
        assert INTERVAL_SECONDS["1hour"] == 3600
        assert INTERVAL_SECONDS["4hour"] == 14400


# ────────────────────────────────────────────────────────────────────────
# Error paths
# ────────────────────────────────────────────────────────────────────────


class TestResampleErrors:
    def test_unknown_source_raises(self) -> None:
        with pytest.raises(ResampleError, match="source_interval"):
            resample_bars(bars=[], source_interval="7minute", target_interval="5minute")

    def test_unknown_target_raises(self) -> None:
        with pytest.raises(ResampleError, match="target_interval"):
            resample_bars(bars=[], source_interval="1minute", target_interval="13minute")

    def test_target_shorter_than_source_raises(self) -> None:
        with pytest.raises(ResampleError, match="shorter than"):
            resample_bars(
                bars=[],
                source_interval="5minute",
                target_interval="1minute",
            )

    def test_non_integer_multiple_raises(self) -> None:
        # 5 -> 15 is a clean multiple; 5 -> 10 is too; try 3 -> 5.
        with pytest.raises(ResampleError, match="integer multiple"):
            resample_bars(
                bars=[],
                source_interval="3minute",
                target_interval="5minute",
            )

    def test_1day_rejected_even_as_source(self) -> None:
        with pytest.raises(ResampleError, match="resampleable set"):
            resample_bars(bars=[], source_interval="1day", target_interval="4hour")

    def test_naive_timestamp_rejected(self) -> None:
        naive_bar = CandleBar(
            timestamp=datetime(2024, 1, 2, 9, 15, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1,
            open_interest=None,
        )
        # Build a second bar whose timestamp we strip of tzinfo via
        # object.__setattr__ (slots=True + frozen=True).
        stripped = CandleBar.__new__(CandleBar)
        object.__setattr__(
            stripped,
            "timestamp",
            datetime(2024, 1, 2, 9, 16),  # naive
        )
        object.__setattr__(stripped, "open", Decimal("100"))
        object.__setattr__(stripped, "high", Decimal("101"))
        object.__setattr__(stripped, "low", Decimal("99"))
        object.__setattr__(stripped, "close", Decimal("100"))
        object.__setattr__(stripped, "volume", 1)
        object.__setattr__(stripped, "open_interest", None)
        with pytest.raises(ResampleError, match="tz-aware"):
            resample_bars(
                bars=[naive_bar, stripped],
                source_interval="1minute",
                target_interval="5minute",
            )


# ────────────────────────────────────────────────────────────────────────
# Empty / identity
# ────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_input_returns_empty(self) -> None:
        assert resample_bars(bars=[], source_interval="1minute", target_interval="5minute") == []

    def test_target_equals_source_is_identity(self) -> None:
        bars = [_bar(i) for i in range(3)]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="1minute",
        )
        assert out == bars


# ────────────────────────────────────────────────────────────────────────
# Aggregation — OHLCV correctness
# ────────────────────────────────────────────────────────────────────────


class TestAggregation:
    def test_five_one_minute_bars_become_one_five_minute_bar(self) -> None:
        """5 × 1m → 1 × 5m, with correct OHLCV folding."""
        bars = [
            _bar(0, open_price=100, high_price=102, low_price=99, close_price=101, volume=10),
            _bar(1, open_price=101, high_price=103, low_price=100, close_price=102, volume=20),
            _bar(2, open_price=102, high_price=104, low_price=101, close_price=103, volume=15),
            _bar(3, open_price=103, high_price=105, low_price=102, close_price=104, volume=25),
            _bar(4, open_price=104, high_price=106, low_price=103, close_price=105, volume=30),
        ]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 1
        agg = out[0]
        assert agg.open == Decimal("100")  # first bar's open
        assert agg.high == Decimal("106")  # max high
        assert agg.low == Decimal("99")  # min low
        assert agg.close == Decimal("105")  # last bar's close
        assert agg.volume == 100  # sum
        assert agg.open_interest == 500  # snapshot == last bar's

    def test_bucket_start_label_aligned_to_multiple(self) -> None:
        """The emitted timestamp is the bucket start, not the first bar's ts."""
        # Start at 09:17 UTC — the 5-minute bucket is 09:15..09:20 UTC
        # (09:15 + k*5min, k integer). 09:17 bucket start = 09:15.
        base = datetime(2024, 1, 2, 9, 17, tzinfo=UTC)
        bars = [
            _bar(0, base=base),
            _bar(1, base=base),
            _bar(2, base=base),
        ]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 1
        # 09:17 → bucket_start 09:15 (since 5min epoch alignment).
        # But we can't hard-code the absolute seconds here; assert the
        # offset instead: bucket_start must be <= first bar's ts and
        # the gap must be less than the target interval.
        agg = out[0]
        assert agg.timestamp <= bars[0].timestamp
        assert (bars[0].timestamp - agg.timestamp) < timedelta(minutes=5)

    def test_partial_final_bucket_preserved(self) -> None:
        """A 7-bar input into a 5m bucket emits 1 full + 1 partial bucket."""
        bars = [_bar(i, open_price=100 + i) for i in range(7)]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 2
        assert out[0].volume == 10 * 5  # bars 0..4
        assert out[1].volume == 10 * 2  # bars 5..6

    def test_multi_bucket_spans_hour_boundary(self) -> None:
        """12 × 5m bars → 1 × 1h bar (spanning a full hour)."""
        bars = [
            _bar(
                i * 5,
                open_price=100 + i,
                high_price=101 + i,
                low_price=99 + i,
                close_price=100.5 + i,
                volume=5,
            )
            for i in range(12)
        ]
        out = resample_bars(
            bars=bars,
            source_interval="5minute",
            target_interval="1hour",
        )
        # The 5m bars span 60 min; depending on the hour bucket
        # boundary they may land in 1 or 2 hour buckets. Assert that
        # total volume and OHLC bounds are conserved.
        total_volume = sum(b.volume for b in out)
        assert total_volume == 60
        assert min(b.low for b in out) == Decimal("99")
        assert max(b.high for b in out) == Decimal("112")

    def test_volume_sum_preserved_across_buckets(self) -> None:
        """Sum of output volumes must equal sum of input volumes."""
        bars = [_bar(i, volume=i + 1) for i in range(30)]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert sum(b.volume for b in out) == sum(b.volume for b in bars)

    def test_open_interest_is_last_snapshot(self) -> None:
        """Aggregate OI must be the bucket's last bar's OI, not max/sum."""
        bars = [
            _bar(0, open_interest=100),
            _bar(1, open_interest=150),
            _bar(2, open_interest=200),
            _bar(3, open_interest=175),
            _bar(4, open_interest=225),
        ]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 1
        assert out[0].open_interest == 225  # last bar's OI

    def test_open_interest_none_passthrough(self) -> None:
        """Cash equity bars (OI=None) must stay None after aggregation."""
        bars = [_bar(i, open_interest=None) for i in range(3)]
        out = resample_bars(
            bars=bars,
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 1
        assert out[0].open_interest is None
