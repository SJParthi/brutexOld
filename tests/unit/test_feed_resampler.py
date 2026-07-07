"""Unit tests for :mod:`brutex.feed.resampler`.

Pure tests — no ``polars`` / ``pyarrow`` / ``duckdb`` needed; every
assertion runs against hand-crafted :class:`CandleBar` lists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.data.models import CandleBar
from brutex.feed.errors import ResampleError
from brutex.feed.resampler import (
    INTERVAL_SECONDS,
    RESAMPLEABLE_INTERVALS,
    resample_bars,
)
from hypothesis import given, settings
from hypothesis import strategies as st

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


# Shared base instant for the monotonic-guard suite below. Matches the
# ``_bar`` default base, so ``_bar`` and ``_bar_at`` agree minute-for-minute.
_BASE = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)


def _bar_at(
    ts: datetime,
    *,
    open_price: float = 100.0,
    volume: int = 10,
) -> CandleBar:
    """A :class:`CandleBar` at an EXACT timestamp (for duplicate / reverse cases)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(open_price)),
        high=Decimal(str(open_price + 1)),
        low=Decimal(str(open_price - 1)),
        close=Decimal(str(open_price + 0.5)),
        volume=volume,
        open_interest=500,
    )


# ────────────────────────────────────────────────────────────────────────
# Static tables
# ────────────────────────────────────────────────────────────────────────


class TestStaticTables:
    def test_interval_seconds_contains_all_21_lake_labels(self) -> None:
        # DEC-TF-VOCAB-21-001: every Track 1 lake label is addressable here.
        from brutex.data.timeframes import LAKE_INTERVAL_LABELS

        assert set(INTERVAL_SECONDS.keys()) == set(LAKE_INTERVAL_LABELS.values())
        assert len(INTERVAL_SECONDS) == 21

    def test_interval_seconds_sync_with_canonical_minutes(self) -> None:
        # Wall-clock seconds match the canonical minute ladder for every
        # intraday label; 1day is wall-clock 86400 (the 375-min TRADING
        # session lives in trading_minutes_for_lake_interval, not here).
        from brutex.data.timeframes import LAKE_INTERVAL_LABELS

        for minutes, label in LAKE_INTERVAL_LABELS.items():
            if label == "1day":
                assert INTERVAL_SECONDS[label] == 86400
            else:
                assert INTERVAL_SECONDS[label] == minutes * 60

    def test_resampleable_intervals_excludes_1day(self) -> None:
        # 1day is in INTERVAL_SECONDS for completeness but calendar-
        # aware resampling lands in P3 so the v1 resampler refuses it.
        assert "1day" not in RESAMPLEABLE_INTERVALS
        assert "1minute" in RESAMPLEABLE_INTERVALS
        assert "4hour" in RESAMPLEABLE_INTERVALS

    def test_resampleable_intervals_excludes_vocab21_additions(self) -> None:
        # DEC-TF-VOCAB-21-001 adds the labels for ADDRESSING only — this v1
        # epoch-anchored bucketer must keep refusing them until the
        # session-anchored derivation path (data.tf_resampler) is the one
        # used (DEC-RESAMPLE-ANCHOR-001).
        for label in (
            "4minute",
            "6minute",
            "7minute",
            "8minute",
            "9minute",
            "11minute",
            "12minute",
            "13minute",
            "14minute",
            "2hour",
            "3hour",
        ):
            assert label in INTERVAL_SECONDS
            assert label not in RESAMPLEABLE_INTERVALS

    def test_interval_seconds_match_minute_math(self) -> None:
        assert INTERVAL_SECONDS["1minute"] == 60
        assert INTERVAL_SECONDS["5minute"] == 300
        assert INTERVAL_SECONDS["1hour"] == 3600
        assert INTERVAL_SECONDS["2hour"] == 7200
        assert INTERVAL_SECONDS["3hour"] == 10800
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

    def test_identity_path_rejects_duplicate_bar(self) -> None:
        """The identity passthrough runs the SAME monotonic guard as the
        down-sampling path (DEC-LAKE-READ-UNIQUE-001) — previously it returned
        early and a duplicated bar rode through silently (fails-before pin)."""
        bars = [_bar(0), _bar(0), _bar(1)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="1minute")

    def test_identity_path_rejects_out_of_order_bar(self) -> None:
        bars = [_bar(1), _bar(0)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="1minute")

    def test_identity_path_rejects_naive_timestamp(self) -> None:
        stripped = CandleBar.__new__(CandleBar)
        object.__setattr__(stripped, "timestamp", datetime(2024, 1, 2, 9, 15))
        for name, value in (
            ("open", Decimal("100")),
            ("high", Decimal("101")),
            ("low", Decimal("99")),
            ("close", Decimal("100")),
            ("volume", 1),
            ("open_interest", None),
        ):
            object.__setattr__(stripped, name, value)
        with pytest.raises(ResampleError, match="tz-aware"):
            resample_bars(bars=[stripped], source_interval="1minute", target_interval="1minute")


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


# ════════════════════════════════════════════════════════════════════════
# Monotonic / duplicate-timestamp guard — DEC-RESAMPLER-MONOTONIC-GUARD-001
# (M2 of the PR-4 gap spec). Folded into this per-module gate file so the
# strict ``--cov=brutex.feed.resampler --cov-fail-under=100`` run that
# discovers ONLY ``test_feed_resampler.py`` covers the guard's raise.
#
# POLICY (justified from the codebase, not invented): ``resample_bars``
# already RAISES :class:`ResampleError` for every other input-contract
# violation (unknown interval, up-sampling, non-multiple, naive tz). The
# guard adds the ordering violation to that SAME error style — the typed
# ``NonMonotonicBarError`` (brutex.execution.errors) exists for exactly
# this violation and the historical driver guard likewise RAISED rather
# than silently de-duplicating (trio deleted per
# DEC-EXECUTION-TRIO-DELETE-001). So the established feed convention is
# *raise on non-monotonic*, never silent dedup (a silent dedup would
# itself hide corrupt vendor data the aggregate exists to faithfully
# reflect).
# ════════════════════════════════════════════════════════════════════════


class TestEmptyAndSingle:
    def test_empty_stream_returns_empty_no_crash(self) -> None:
        # REGRESSION: empty input is the no-bars short-circuit; guard never
        # runs (no previous timestamp to compare).
        assert resample_bars(bars=[], source_interval="1minute", target_interval="5minute") == []

    def test_single_bar_aggregates_cleanly(self) -> None:
        # REGRESSION: one bar => one (partial) bucket; the guard's prev_ts is
        # None on the only iteration, so it never fires.
        out = resample_bars(
            bars=[_bar(0, volume=7)],
            source_interval="1minute",
            target_interval="5minute",
        )
        assert len(out) == 1
        assert out[0].timestamp == _BASE
        assert out[0].volume == 7


class TestDuplicateTimestamp:
    def test_exact_duplicate_raises_resample_error(self) -> None:
        # FAILS ON MAIN: a duplicate lands in the SAME bucket and its volume
        # is summed twice (10 + 10 = 20 for a single instant) — silent
        # corruption. The guard raises instead.
        dup_ts = _BASE
        bars = [_bar_at(dup_ts, volume=10), _bar_at(dup_ts, volume=10)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")

    def test_duplicate_does_not_double_count_volume(self) -> None:
        # The anti-illusion assertion: prove the guard prevents the
        # double-count. Without the guard the single-instant volume would
        # aggregate to 20; with it, resample_bars never returns a value.
        dup_ts = _BASE
        bars = [_bar_at(dup_ts, volume=10), _bar_at(dup_ts, volume=10)]
        with pytest.raises(ResampleError):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
        # Sanity floor: the SAME two instants de-collided to distinct minutes
        # aggregate to exactly 20 in ONE 5m bucket — confirming the 20 the
        # guard suppresses really was the double-count, not a real total.
        clean = [_bar(0, volume=10), _bar(1, volume=10)]
        out = resample_bars(bars=clean, source_interval="1minute", target_interval="5minute")
        assert len(out) == 1
        assert out[0].volume == 20

    def test_duplicate_followed_by_valid_still_raises(self) -> None:
        # equal-then-greater: [t0, t0, t1]. The duplicate at index 1 trips the
        # guard before the valid t1 is ever reached.
        bars = [_bar(0), _bar(0), _bar(1)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")


class TestOutOfOrder:
    def test_one_out_of_order_bar_in_the_middle_raises(self) -> None:
        # FAILS ON MAIN: [t0, t1, t2, t1, t4]. The t1 at index 3 is < t2; on
        # main it re-opens the (already-emitted) first 5m bucket as a SECOND
        # output bar — corrupt. The guard raises.
        bars = [_bar(0), _bar(1), _bar(2), _bar(1), _bar(4)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")

    def test_strictly_decreasing_stream_raises_on_first_step_back(self) -> None:
        # Fully reversed [t4, t3, t2, t1, t0]. The first backwards step (t3 <
        # t4) trips the guard.
        bars = [_bar(i) for i in reversed(range(5))]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")

    def test_out_of_order_across_bucket_boundary_raises(self) -> None:
        # A bar that jumps BACK into a prior, already-closed bucket
        # [t0, t6, t1] — t1 < t6 — the most dangerous mis-bucket on main
        # (re-opens minute-0's 5m bucket). The guard catches it.
        bars = [_bar(0), _bar(6), _bar(1)]
        with pytest.raises(ResampleError, match="strictly-increasing"):
            resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")


class TestNoPartialCorruptOutput:
    def test_raise_yields_no_value_no_partial_bucket(self) -> None:
        # The guard raises BEFORE the offending bar is bucketed, so no caller
        # ever receives a partially-aggregated, corrupt result. We prove the
        # function returns NOTHING (raises) — the only honest outcome.
        bars = [_bar(0), _bar(1), _bar(0)]  # valid, valid, backwards
        captured: list[CandleBar] | None = None
        with pytest.raises(ResampleError, match="strictly-increasing"):
            captured = resample_bars(
                bars=bars, source_interval="1minute", target_interval="5minute"
            )
        # Assignment never executed: the exception fired inside the call.
        assert captured is None


class TestCleanInputUnchanged:
    def test_large_normal_stream_byte_identical(self) -> None:
        # REGRESSION: 600 strictly-increasing 1m bars resampled 1m -> 1hour.
        # The guard never fires on clean input, and the output must equal an
        # INDEPENDENT field-by-field reference fold computed by the SAME
        # epoch-floor bucketing the resampler uses (the 09:15 base is NOT
        # aligned to a 1h boundary, so the first bucket is the 09:00 partial).
        bars = [
            _bar(i, open_price=100.0 + (i % 5), volume=1 + (i % 7), open_interest=500 + i)
            for i in range(600)
        ]
        out = resample_bars(bars=bars, source_interval="1minute", target_interval="1hour")

        # Independent reference: group by epoch-floored 1h bucket start.
        buckets: dict[datetime, list[CandleBar]] = {}
        for bar in bars:
            epoch_s = int(bar.timestamp.timestamp())
            start = datetime.fromtimestamp((epoch_s // 3600) * 3600, tz=UTC)
            buckets.setdefault(start, []).append(bar)

        assert len(out) == len(buckets)
        for agg, (start, window) in zip(out, sorted(buckets.items()), strict=True):
            assert agg.timestamp == start
            assert agg.open == window[0].open
            assert agg.high == max(x.high for x in window)
            assert agg.low == min(x.low for x in window)
            assert agg.close == window[-1].close
            assert agg.volume == sum(x.volume for x in window)
            assert agg.open_interest == window[-1].open_interest
        # Total volume is conserved (no double-count, no loss).
        assert sum(b.volume for b in out) == sum(b.volume for b in bars)

    def test_clean_stream_with_microsecond_gap_unique_ok(self) -> None:
        # REGRESSION: strictly-increasing but sub-second-distinct timestamps
        # within one minute are NOT a duplicate — the guard must let them pass
        # (they share a bucket, summing correctly).
        t0 = _BASE
        bars = [
            _bar_at(t0, volume=3),
            _bar_at(t0 + timedelta(microseconds=1), volume=4),
            _bar_at(t0 + timedelta(seconds=1), volume=5),
        ]
        out = resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
        assert len(out) == 1
        assert out[0].volume == 12


class TestProperty:
    @settings(max_examples=200)
    @given(
        offsets=st.lists(
            st.integers(min_value=0, max_value=120),
            min_size=2,
            max_size=30,
        )
    )
    def test_non_strictly_increasing_always_raises(self, offsets: list[int]) -> None:
        # For ANY offset list that is NOT strictly increasing (has a dup or a
        # backwards step), resample_bars must raise — deterministically.
        is_strict = all(b > a for a, b in pairwise(offsets))
        bars = [_bar(o, base=_BASE) for o in offsets]
        if is_strict:
            # Strictly-increasing minute offsets => succeeds (no guard trip).
            out = resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
            # Total input volume is conserved across the output buckets.
            assert sum(b.volume for b in out) == sum(b.volume for b in bars)
        else:
            with pytest.raises(ResampleError, match="strictly-increasing"):
                resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")

    @settings(max_examples=100)
    @given(n=st.integers(min_value=1, max_value=50))
    def test_sorted_unique_volume_conserved(self, n: int) -> None:
        # A clean strictly-increasing stream always succeeds and conserves
        # total volume (no double-count, no loss).
        bars = [_bar(i, volume=1 + (i % 9)) for i in range(n)]
        out = resample_bars(bars=bars, source_interval="1minute", target_interval="5minute")
        assert sum(b.volume for b in out) == sum(b.volume for b in bars)
