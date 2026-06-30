"""Monotonic / duplicate-timestamp guard for :func:`brutex.feed.resampler.resample_bars`.

DEC-RESAMPLER-MONOTONIC-GUARD-001 (M2 of the PR-4 gap spec): the v1
resampler trusts that its input is sorted **strictly ascending** and
re-sorts nothing. Before the guard a non-increasing timestamp — an
out-of-order bar OR an exact duplicate — was silently mis-bucketed:
either double-counting the offending bar's volume into a bucket or
opening a spurious second output bucket for an already-emitted window,
corrupting the aggregated OHLCV. This is the symmetric hazard to the
``execution/driver`` feed-integrity guard, which already rejects
``this_ts <= prev_ts`` with :class:`NonMonotonicBarError`.

POLICY (justified from the codebase, not invented):
``resample_bars`` already RAISES :class:`ResampleError` for every other
input-contract violation (unknown interval, up-sampling, non-multiple,
naive tz). The guard adds the ordering violation to that SAME error
style — and the sibling feed-integrity guard in
:meth:`brutex.execution.driver.BacktestDriver.run` likewise RAISES
(``NonMonotonicBarError``) rather than silently de-duplicating. So the
established feed convention is *raise on non-monotonic*, never silent
dedup. A silent dedup here would itself be an illusion (it would hide
corrupt vendor data the aggregate exists to faithfully reflect).

Every assertion drives the REAL ``resample_bars`` — no mocks. Each test
is annotated with whether it FAILS on origin/main (silent corruption)
or is a pure regression.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest
from brutex.data.models import CandleBar
from brutex.feed.errors import ResampleError
from brutex.feed.resampler import resample_bars

_BASE = datetime(2024, 1, 2, 9, 15, tzinfo=UTC)


def _bar(
    minute_offset: int,
    *,
    open_price: float = 100.0,
    volume: int = 10,
    open_interest: int | None = 500,
    base: datetime | None = None,
) -> CandleBar:
    """A deterministic 1-minute :class:`CandleBar` *minute_offset* min after *base*."""
    base_ts = base or _BASE
    return CandleBar(
        timestamp=base_ts + timedelta(minutes=minute_offset),
        open=Decimal(str(open_price)),
        high=Decimal(str(open_price + 1)),
        low=Decimal(str(open_price - 1)),
        close=Decimal(str(open_price + 0.5)),
        volume=volume,
        open_interest=open_interest,
    )


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
# Deterministic empty / single-bar (no crash, no guard false-positive)
# ────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────
# Duplicate timestamps  (FAILS on main: volume double-counted)
# ────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────
# Out-of-order timestamps  (FAILS on main: spurious 2nd bucket / wrong OHLC)
# ────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────
# No partial corrupt output is observable before the raise
# ────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────
# Regression: clean sorted/unique input is byte-for-byte unchanged
# ────────────────────────────────────────────────────────────────────────


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


# ────────────────────────────────────────────────────────────────────────
# Property: shuffled-with-dups always raises; a sorted-unique permutation
# always succeeds (deterministic per the raise policy).
# ────────────────────────────────────────────────────────────────────────

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


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
